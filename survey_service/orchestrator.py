"""Orchestrator: 한 턴을 처리한다(docs/rules/orchestrator.md, preconditions.md, timing.md).

판단은 하지 않는다. 처리 표에 따라 turn-engine과 모델 세션을 순서대로 부르고, 저장소에서 정해진 범위를 가져와 건네고,
쓰기를 확정하고, 확정 뒤의 일을 서로의 성패와 무관하게 한다. 공개 API(I10)는 O06 대기라 handle_turn만 낸다.
"""
from __future__ import annotations

import copy
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from boundary_contracts import canonical

from . import assembly
from .errors import ReleaseUnavailable, StoreError, TurnIdConflict
from .ports import (Engine, FrontendSink, LoadedRelease, ReleaseProvider, ResultDispatcher, StaffConsole, TraceSink,
                    UnattachedDispatcher, UnconfiguredStaffConsole, default_engine)
from .store import effects
from .store.base import SessionStorePort

log = logging.getLogger("survey_service.orchestrator")

SCHEMA_CONTRACT_VERSION = 1  # TurnTrace.contract_version. 이 서비스가 지원하는 문진 스키마 정수 버전

IN_KINDS = {"SESSION_START": "SESSION_START", "UTTERANCE": "UTTERANCE", "SESSION_END": "SESSION_END"}


@dataclass(frozen=True)
class Route:
    """처리 표(9-1)의 한 칸."""
    turn_kind: Optional[str]      # None이면 처리 안 함
    create: bool = False          # 세션 정보 생성
    full: bool = False            # LoRA-A~저장 판단까지 거치는 전체 흐름
    keep_log: bool = True         # 처리 안 함일 때 대화 로그를 남기는가


def route(input_kind: str, session_found: bool, status: Optional[str]) -> Route:
    """input_kind: SESSION_START | UTTERANCE | RESUME | END | SESSION_END."""
    ignore_nolog = Route(None, keep_log=False)
    if not session_found:
        if input_kind == "SESSION_START":
            return Route("SESSION_START", create=True)
        if input_kind == "UTTERANCE":
            return Route("UTTERANCE", create=True, full=True)
        return ignore_nolog
    if status and status.startswith("ENDED"):
        return ignore_nolog
    if status == "PAUSED_RISK":
        if input_kind == "RESUME":
            return Route("RESUME")
        if input_kind == "END":
            return Route("INTERVENTION_END")
        if input_kind == "SESSION_END":
            return Route("END_LEFT")
        return Route(None, keep_log=True)         # 멈춘 동안의 말은 직원이 확인할 수 있게 남긴다(G5)
    # IN_PROGRESS
    if input_kind == "SESSION_START":
        return Route("SESSION_START")
    if input_kind == "UTTERANCE":
        return Route("UTTERANCE", full=True)
    if input_kind == "END":
        return Route("INTERVENTION_END")
    if input_kind == "SESSION_END":
        return Route("END_LEFT")
    return Route(None, keep_log=True)             # 진행 중 세션에 RESUME: 뜻이 없다(G6)


@dataclass
class TurnOutcome:
    orchestrator_output: dict
    frontend_input: Optional[dict]
    staff_console_input: Optional[dict]
    trace: dict
    release_id: Optional[str]
    replayed: bool = False
    cacheable: bool = True      # 쓰기 반영 여부를 알 수 없는 턴(write_outcome UNKNOWN)은 결과를 고정하지 않는다


class _SessionLocks:
    """세션별 락. 쓰는 동안만 항목을 둔다."""
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, list] = {}

    @contextmanager
    def hold(self, key: str):
        with self._guard:
            ent = self._locks.setdefault(key, [threading.Lock(), 0])
            ent[1] += 1
        try:
            with ent[0]:
                yield
        finally:
            with self._guard:
                ent[1] -= 1
                if ent[1] == 0:
                    self._locks.pop(key, None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Turn:
    """한 턴 동안의 상태와 Trace 재료."""
    def __init__(self, session_id: str, turn_id: str) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.failed: list[dict] = []
        self.trace: dict[str, Any] = {k: None for k in TRACE_KEYS}
        self.trace.update(session_id=session_id, turn_id=turn_id, contract_version=SCHEMA_CONTRACT_VERSION)
        self.release: Optional[LoadedRelease] = None
        self.state: Optional[dict] = None          # 턴 시작 문서
        self.revision: Any = None
        self.created: Optional[dict] = None
        self.route: Optional[Route] = None
        self.parser_out = self.norm_out = self.judge_out = self.writer_out = self.plan_out = None
        self.committed = False
        self.commit_unknown = False
        self.staff_status = "NONE"                 # NONE | DELIVERED | DELIVERY_FAILED
        self.staff_delivery = "NOT_APPLICABLE"     # 로그의 staff_alert_delivery
        self.response_text: Optional[str] = None
        self.signal: Optional[dict] = None
        self.intervention_result: Optional[dict] = None
        self.dispatch_called = False
        self.console_delivered = False
        self.console_calls: list = []
        self.post_doc: Optional[dict] = None
        self.input_hash = ""
        self.console: dict = {"staff_alert": None, "turn_failure_signal": None, "intervention_result": None}

    def fail(self, phase: str, step: str) -> None:
        self.failed.append({"phase": phase, "step": step})


TRACE_KEYS = ["frontend_message", "intervention_decision", "orchestrator_input", "parser_input", "parser_output",
              "normalizer_input", "normalizer_output", "judge_input", "judge_output", "record_writer_input",
              "record_writer_output", "planner_input", "planner_output", "generator_input", "generator_output",
              "frontend_input", "staff_console_input", "dispatch_input", "dispatch_output", "orchestrator_output",
              "session_before", "session_after", "log_entry", "persistence_load_request", "persistence_load_result",
              "persistence_commit_request", "persistence_commit_result"]


class Orchestrator:
    def __init__(self, store: SessionStorePort, releases: ReleaseProvider, *, engine: Optional[Engine] = None,
                 staff_console: Optional[StaffConsole] = None, dispatcher: Optional[ResultDispatcher] = None,
                 frontend_sink: Optional[FrontendSink] = None, trace_sink: Optional[TraceSink] = None) -> None:
        self.store = store
        self.releases = releases
        self.engine = engine or default_engine()
        self.staff_console = staff_console or UnconfiguredStaffConsole()
        self.dispatcher = dispatcher or UnattachedDispatcher()
        self.frontend_sink = frontend_sink
        self.trace_sink = trace_sink
        self._locks = _SessionLocks()
        self.counters: dict[str, int] = {}

    # ------------------------------------------------------------------ 공개
    def handle_turn(self, *, turn_id: str, frontend_message: Optional[Mapping] = None,
                    intervention_decision: Optional[Mapping] = None) -> TurnOutcome:
        """한 턴을 처리한다. 같은 (세션, 턴 ID)는 저장한 결과를 그대로 돌려준다(이중 저장 없음).

        frontend_message(FrontendMessage)와 intervention_decision(InterventionDecision) 중 정확히 하나를 준다.
        입력이 계약을 어기면 ValueError(턴 실패가 아니라 호출 오류).
        """
        if (frontend_message is None) == (intervention_decision is None):
            raise ValueError("frontend_message와 intervention_decision 중 정확히 하나를 줘야 한다")
        src = frontend_message if frontend_message is not None else intervention_decision
        sid = src.get("session_id") if isinstance(src, Mapping) else None
        if not isinstance(sid, str) or not sid or not isinstance(turn_id, str) or not turn_id:
            raise ValueError("session_id와 turn_id는 비어 있지 않은 문자열이어야 한다")
        if frontend_message is not None:
            if frontend_message.get("kind") not in IN_KINDS:
                raise ValueError("알 수 없는 FrontendMessage.kind")
            if frontend_message["kind"] == "UTTERANCE" and not isinstance(frontend_message.get("utterance"), str):
                raise ValueError("UTTERANCE에는 utterance가 있어야 한다")
        elif intervention_decision.get("decision") not in ("RESUME", "END"):
            raise ValueError("알 수 없는 InterventionDecision.decision")
        input_hash = canonical.sha256_hash({"m": frontend_message, "d": intervention_decision})
        with self._locks.hold(sid):
            replay = self._replay(sid, turn_id, input_hash)
            if replay is not None:
                return replay
            out = self._process(sid, turn_id, frontend_message, intervention_decision, input_hash)
            try:
                if out.cacheable:
                    self.store.save_turn_result(sid, turn_id, input_hash, {
                        "orchestrator_output": out.orchestrator_output, "frontend_input": out.frontend_input,
                        "staff_console_input": out.staff_console_input, "release_id": out.release_id})
            except StoreError:
                log.error("턴 결과를 멱등 기록에 남기지 못했다 (session=%s turn=%s)", sid, turn_id)
            self._submit_trace(out)
            return out

    # ------------------------------------------------------------------ 멱등
    def _replay(self, sid: str, turn_id: str, input_hash: str) -> Optional[TurnOutcome]:
        try:
            prev = self.store.get_turn(sid, turn_id)
        except Exception:
            return None
        if prev is None:
            return None
        if prev.input_hash != input_hash:
            raise TurnIdConflict("같은 턴 ID에 다른 입력이 왔다")
        if prev.result is not None:
            r = prev.result
            return TurnOutcome(r["orchestrator_output"], r["frontend_input"], r["staff_console_input"], trace={},
                               release_id=r.get("release_id"), replayed=True)
        # 쓰기는 확정됐는데 결과를 남기기 전에 중단됐다. 쓰기를 다시 하지 않는다. 응답 문장은 다시 만들지 않는다(설계 4절).
        signal = {"session_id": sid, "write_committed": "COMMITTED", "staff_alert": "DELIVERY_FAILED"}
        oo = {"turn_kind": None, "session_info_created": None, "log_entry": None, "turn_failure_signal": signal,
              "intervention_result": None, "dispatch_called": False}
        return TurnOutcome(oo, {"response_text": None, "turn_failure_signal": signal}, None, trace={}, release_id=None,
                           replayed=True)

    # ------------------------------------------------------------------ 처리
    def _process(self, sid: str, turn_id: str, message: Optional[Mapping], decision: Optional[Mapping],
                 input_hash: str) -> TurnOutcome:
        t = _Turn(sid, turn_id)
        t.input_hash = input_hash
        t.trace["frontend_message"] = copy.deepcopy(message)
        t.trace["intervention_decision"] = copy.deepcopy(decision)
        if message is not None:
            input_kind, in_kind = message["kind"], message["kind"]
            utterance = message.get("utterance")
        else:
            input_kind, in_kind, utterance = decision["decision"], "INTERVENTION", None

        # 턴 맨 앞: 세션이 이미 있는지 찾는다
        t.trace["persistence_load_request"] = {"session_id": sid}
        loaded = self.store.load(sid)
        t.trace["persistence_load_result"] = copy.deepcopy(loaded)
        if loaded["status"] == "DB_ERROR":
            return self._finish_without_session(t, decision, no_route=True)
        found = loaded["status"] == "OK"
        status = loaded["state"]["progress_record"]["session_status"] if found else None
        latest = self.releases.active()
        t.trace["orchestrator_input"] = {
            "frontend_input": copy.deepcopy(message), "intervention_decision": copy.deepcopy(decision),
            "session_found": found,
            "session_question_bank_version": loaded["state"]["session_info"]["question_bank_version"] if found else None,
            "session_status": status,
            "latest_question_bank_version": latest.bank["version"] if latest else 0}
        t.route = rt = route(input_kind, found, status)

        if rt.turn_kind is None:
            return self._ignored(t, loaded, in_kind, utterance, decision)

        # 릴리스 결정과 세션 정보 생성
        try:
            if found:
                rid = self.store.release_id_of(sid)
                t.release = self.releases.get(rid)
                t.state, t.revision = loaded["state"], loaded["revision"]
            else:
                if latest is None:
                    raise ReleaseUnavailable("활성 릴리스가 없다")
                t.release = latest
                state0 = effects.initial_state(sid, latest.bank)
                t.revision = self.store.create(sid, latest.release_id, state0)
                t.state, t.created = state0, copy.deepcopy(state0["session_info"])
        except ReleaseUnavailable:
            if not found:
                return self._finish_without_session(t, decision)
            # 세션이 묶인 릴리스를 못 쓴다: 첫 단계 실패로 처리한다(설계 5절)
            t.state, t.revision = loaded["state"], loaded["revision"]
            t.release = None
            t.fail("BEFORE_COMMIT", "LoRA-A")
            return self._finalize(t, in_kind, utterance, decision, bank_version=t.state["session_info"]["question_bank_version"])
        except StoreError:
            return self._finish_without_session(t, decision)

        t.trace["session_before"] = copy.deepcopy(t.state)
        self._pipeline(t, utterance)
        self._after_commit(t, utterance)
        return self._finalize(t, in_kind, utterance, decision, bank_version=t.release.bank["version"] if t.release else None)

    # ------------------------------------------------------------------ 처리 안 함
    def _ignored(self, t: _Turn, loaded: dict, in_kind: str, utterance: Optional[str], decision: Optional[Mapping]) -> TurnOutcome:
        if decision is not None:
            t.intervention_result = {"session_id": t.session_id, "result": "IGNORED"}
        t.state = loaded["state"]
        t.revision = loaded["revision"]
        if loaded["status"] == "OK":
            t.trace["session_before"] = copy.deepcopy(t.state)
            t.release = self._try_release(t.session_id)
        self._deliver_console_final(t)
        entry = None
        if t.route.keep_log and t.state is not None:
            entry = self._log_entry(t, in_kind, utterance, decision, bank_version=t.state["session_info"]["question_bank_version"],
                                    handled=None)
            self._append_log(t, entry)
        oo = {"turn_kind": None, "session_info_created": None, "log_entry": entry, "turn_failure_signal": None,
              "intervention_result": t.intervention_result, "dispatch_called": False}
        # 프론트가 보낸 말이 처리되지 않았으면 '할 말 없음'을 돌려준다(응답도 신호도 없다). 개입 결정에는 프론트가 없다.
        fi = {"response_text": None, "turn_failure_signal": None} if decision is None else None
        return self._outcome(t, oo, fi)

    def _try_release(self, sid: str) -> Optional[LoadedRelease]:
        try:
            rid = self.store.release_id_of(sid)
            return self.releases.get(rid) if rid else None
        except Exception:
            return None

    # ------------------------------------------------------------------ 세션을 못 만든 턴
    def _finish_without_session(self, t: _Turn, decision: Optional[Mapping], no_route: bool = False) -> TurnOutcome:
        """세션 정보 생성 실패, 조회 실패: 붙일 세션이 없어 로그를 남기지 않는다(9-1 3항)."""
        t.signal = {"session_id": t.session_id, "write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}
        if decision is not None:
            t.intervention_result = {"session_id": t.session_id, "result": "FAILED"}
        self._deliver_console_final(t)
        kind = None if no_route else (t.route.turn_kind if t.route else None)
        oo = {"turn_kind": kind, "session_info_created": None, "log_entry": None, "turn_failure_signal": t.signal,
              "intervention_result": t.intervention_result, "dispatch_called": False}
        fi = {"response_text": None, "turn_failure_signal": t.signal}
        return self._outcome(t, oo, fi)

    # ------------------------------------------------------------------ 쓰기 확정까지
    def _step(self, t: _Turn, step: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, fn()
        except Exception as e:  # 컴포넌트가 던진 것은 모두 그 단계의 실패다. 값은 남기지 않는다.
            t.fail("BEFORE_COMMIT", step)
            log.warning("턴 단계 실패 session=%s turn=%s step=%s error=%s", t.session_id, t.turn_id, step, type(e).__name__)
            self._count(f"step_failed.{step}")
            return False, None

    def _pipeline(self, t: _Turn, utterance: Optional[str]) -> None:
        bank = assembly.Bank(t.release.bank) if t.release else None
        doc = t.state
        rt = t.route
        kind = rt.turn_kind
        store_result = effects.empty_store_result()
        judgments: list = []
        utter_judgment = None
        store_summary = None
        records_after = doc["answer_records"]
        if rt.full:
            turn_index = len(doc["dialogue_log"])
            pin = assembly.parser_input(utterance, doc, bank)
            t.trace["parser_input"] = pin
            ok, t.parser_out = self._step(t, "LoRA-A", lambda: t.release.session.parse(pin))
            t.trace["parser_output"] = copy.deepcopy(t.parser_out)
            if not ok:
                return
            nin = assembly.normalizer_input(t.parser_out["items"], doc, bank, turn_index)
            t.trace["normalizer_input"] = nin
            ok, t.norm_out = self._step(t, "NORMALIZE", lambda: self.engine.normalize(nin))
            t.trace["normalizer_output"] = copy.deepcopy(t.norm_out)
            if not ok:
                return
            jin = assembly.judge_input(t.parser_out["items"], t.norm_out, utterance, doc, bank)
            t.trace["judge_input"] = jin
            ok, t.judge_out = self._step(t, "JUDGE", lambda: self.engine.judge(jin))
            t.trace["judge_output"] = copy.deepcopy(t.judge_out)
            if not ok:
                return
            win = assembly.writer_input(t.judge_out, doc, bank)
            t.trace["record_writer_input"] = win

            def write_and_apply():
                res = self.engine.write_record(win)
                return res, effects.apply_store_result(doc["answer_records"], res)

            ok, wr = self._step(t, "RECORD_WRITE", write_and_apply)
            if not ok:
                return
            t.writer_out, records_after = wr
            t.trace["record_writer_output"] = copy.deepcopy(t.writer_out)
            store_result, judgments = t.writer_out, t.judge_out["judgments"]
            utter_judgment, store_summary = t.judge_out["utterance_judgment"], t.writer_out["store_summary"]

        pl_in = assembly.planner_input(kind, utter_judgment, judgments, store_summary, records_after, doc, bank)
        t.trace["planner_input"] = pl_in
        ok, t.plan_out = self._step(t, "PLAN", lambda: self.engine.plan(pl_in))
        t.trace["planner_output"] = copy.deepcopy(t.plan_out)
        if not ok:
            return

        writes = {"store_result": store_result, "progress_record_update": t.plan_out["progress_record_update"],
                  "planner_record_update": t.plan_out["planner_record_update"]}
        req = {"session_id": t.session_id, "expected_revision": t.revision, "writes": writes}
        t.trace["persistence_commit_request"] = copy.deepcopy(req)
        res = self.store.commit(req, turn_id=t.turn_id, input_hash=t.input_hash)
        t.trace["persistence_commit_result"] = copy.deepcopy(res)
        if res["status"] == "OK":
            t.committed = True
            t.revision = res["revision"]
            t.post_doc = effects.apply_writes(doc, writes)
        else:
            t.fail("BEFORE_COMMIT", "COMMIT")
            t.commit_unknown = res.get("write_outcome") == "UNKNOWN"
            self._count("commit_failed." + res["status"])

    # ------------------------------------------------------------------ 쓰기 확정 뒤
    def _after_commit(self, t: _Turn, utterance: Optional[str]) -> None:
        if not t.committed:
            return  # 쓰기 확정이 실패하면 확정 뒤의 일은 하나도 하지 않는다(preconditions 0-1.3)
        po = t.plan_out
        if t.route.turn_kind in ("RESUME", "INTERVENTION_END"):
            t.intervention_result = {"session_id": t.session_id, "result": "APPLIED"}
        alert = po.get("staff_alert")
        # 직원 알림은 지연 없이: 응답 생성보다 앞에서 한다
        if alert is not None:
            t.console["staff_alert"] = {"session_id": t.session_id, "staff_alert": copy.deepcopy(alert)}
        t.console["intervention_result"] = t.intervention_result
        if alert is not None or t.intervention_result is not None:
            t.console_calls.append(copy.deepcopy(t.console))
            try:
                self.staff_console.deliver(copy.deepcopy(t.console))
                t.console_delivered = True
                if alert is not None:
                    t.staff_status, t.staff_delivery = "DELIVERED", "SUCCESS"
            except Exception as e:
                log.warning("직원 콘솔 전달 실패 session=%s turn=%s error=%s", t.session_id, t.turn_id, type(e).__name__)
                self._count("step_failed.DELIVER_STAFF")
                t.fail("AFTER_COMMIT", "DELIVER_STAFF")
                if alert is not None:
                    t.staff_status, t.staff_delivery = "DELIVERY_FAILED", "FAILURE"
                    t.console["staff_alert"] = None
        # 결과 전달(문진이 끝나는 턴)
        if po["flow"] == "END":
            self._dispatch(t)
        # 응답 생성
        directive = po["directive"]
        gin = assembly.generator_input(directive, utterance)
        t.trace["generator_input"] = gin
        try:
            gen = t.release.session.generate(gin)
            t.trace["generator_output"] = copy.deepcopy(gen)
            t.response_text = gen["response_text"]
        except Exception as e:
            log.warning("응답 생성 실패 session=%s turn=%s error=%s", t.session_id, t.turn_id, type(e).__name__)
            self._count("step_failed.LoRA-B")
            t.fail("AFTER_COMMIT", "LoRA-B")

    def _dispatch(self, t: _Turn) -> None:
        din = assembly.dispatch_input(t.post_doc)
        t.trace["dispatch_input"] = din
        t.dispatch_called = True
        try:
            dout = dict(self.dispatcher.dispatch(copy.deepcopy(din)))
            ok = bool(dout.get("dispatch_record", {}).get("succeeded"))
        except Exception as e:
            log.warning("결과 전달 실패 session=%s turn=%s error=%s", t.session_id, t.turn_id, type(e).__name__)
            dout, ok = {"dispatch_record": {"succeeded": False}}, False
        t.trace["dispatch_output"] = copy.deepcopy(dout)
        try:
            self.store.append_dispatch(t.session_id, {"succeeded": ok})
        except StoreError:
            log.error("전달 기록을 남기지 못했다 session=%s turn=%s", t.session_id, t.turn_id)

    # ------------------------------------------------------------------ 로그와 신호
    def _signal(self, t: _Turn) -> Optional[dict]:
        if not t.failed:
            return None
        return {"session_id": t.session_id, "write_committed": "COMMITTED" if t.committed else "NOT_COMMITTED",
                "staff_alert": t.staff_status}

    def _finalize(self, t: _Turn, in_kind: str, utterance: Optional[str], decision: Optional[Mapping],
                  bank_version: Optional[int]) -> TurnOutcome:
        if decision is not None and not t.committed:
            t.intervention_result = {"session_id": t.session_id, "result": "FAILED"}
        # 프론트 전달(O06 대기: 자리만 있다)
        t.signal = self._signal(t)
        fi_text = t.response_text
        if t.committed and self.frontend_sink is not None and t.response_text is not None:
            try:
                self.frontend_sink.deliver({"response_text": t.response_text, "turn_failure_signal": t.signal})
            except Exception as e:
                log.warning("프론트 전달 실패 session=%s turn=%s error=%s", t.session_id, t.turn_id, type(e).__name__)
                self._count("step_failed.DELIVER_FRONT")
                t.fail("AFTER_COMMIT", "DELIVER_FRONT")
                t.signal = self._signal(t)
                fi_text = None
        fi = {"response_text": fi_text, "turn_failure_signal": t.signal}
        self._deliver_console_final(t)
        entry = self._log_entry(t, in_kind, utterance, decision, bank_version, handled=t.route.turn_kind)
        self._append_log(t, entry)
        t.trace["frontend_input"] = copy.deepcopy(fi)
        oo = {"turn_kind": t.route.turn_kind, "session_info_created": t.created, "log_entry": entry,
              "turn_failure_signal": t.signal, "intervention_result": t.intervention_result,
              "dispatch_called": t.dispatch_called}
        return self._outcome(t, oo, fi)

    def _deliver_console_final(self, t: _Turn) -> None:
        """직원 콘솔에 아직 못 나간 실패 신호·개입 결과를 한 번 더 보낸다(이미 나간 알림은 되풀이하지 않는다).
        마지막으로 보낸 내용이 이 턴의 StaffConsoleInput(출력, Trace)이다."""
        payload = {"staff_alert": None if t.console_delivered else t.console["staff_alert"],
                   "turn_failure_signal": t.signal,
                   "intervention_result": None if t.console_delivered else t.intervention_result}
        if payload["turn_failure_signal"] is None and payload["intervention_result"] is None:
            return
        t.console = payload
        t.console_calls.append(copy.deepcopy(payload))
        try:
            self.staff_console.deliver(copy.deepcopy(payload))
        except Exception as e:
            log.warning("직원 콘솔 신호 전달 실패 session=%s turn=%s error=%s", t.session_id, t.turn_id, type(e).__name__)

    def _log_entry(self, t: _Turn, in_kind: str, utterance: Optional[str], decision: Optional[Mapping],
                   bank_version: Optional[int], handled: Optional[str]) -> dict:
        po = t.plan_out
        if t.failed:
            result: Any = {"failed_steps": copy.deepcopy(t.failed)}
        elif handled is None:
            result = "NOT_APPLICABLE"
        else:
            result = "COMMITTED"
        return {
            "input": {"kind": in_kind, "utterance": utterance, "intervention_decision": decision["decision"] if decision else None},
            "handling": {"handled": handled} if handled else "IGNORED",
            "question_bank_version": bank_version,
            "turn_result": result,
            "parser_output": copy.deepcopy(t.parser_out), "normalizer_output": copy.deepcopy(t.norm_out),
            "judge_output": copy.deepcopy(t.judge_out), "record_writer_output": copy.deepcopy(t.writer_out),
            "directive": copy.deepcopy(po["directive"]) if po else None,
            "flow": po["flow"] if po else None,
            "progress_record_update": copy.deepcopy(po["progress_record_update"]) if po else None,
            "staff_alert": copy.deepcopy(po["staff_alert"]) if po and t.committed else None,
            "response_text": t.response_text,
            "staff_alert_delivery": t.staff_delivery,
            "turn_failure_signal": t.signal,
            "intervention_result": t.intervention_result,
        }

    def _append_log(self, t: _Turn, entry: dict) -> None:
        try:
            self.store.append_log(t.session_id, entry)
        except StoreError:
            log.error("대화 로그를 남기지 못했다 session=%s turn=%s", t.session_id, t.turn_id)
            self._count("log_append_failed")

    # ------------------------------------------------------------------ 결과 조립
    def _outcome(self, t: _Turn, oo: dict, fi: Optional[dict]) -> TurnOutcome:
        sc = t.console if any(v is not None for v in t.console.values()) else None
        t.trace["orchestrator_output"] = copy.deepcopy(oo)
        t.trace["log_entry"] = copy.deepcopy(oo["log_entry"])
        t.trace["staff_console_input"] = copy.deepcopy(sc)
        if fi is not None and t.trace["frontend_input"] is None:
            t.trace["frontend_input"] = copy.deepcopy(fi)
        after = self.store.load(t.session_id)
        if after["status"] == "OK":
            t.trace["session_after"] = after["state"]
        if t.state is not None:
            t.trace["turn_index"] = len(t.state["dialogue_log"])
        rid = t.release.release_id if t.release else (self.store.release_id_of(t.session_id) or self._active_id())
        return TurnOutcome(oo, fi, copy.deepcopy(sc), t.trace, rid, cacheable=not t.commit_unknown)

    def _active_id(self) -> Optional[str]:
        a = self.releases.active()
        return a.release_id if a else None

    def _submit_trace(self, out: TurnOutcome) -> None:
        if self.trace_sink is None or out.release_id is None:
            return
        try:
            self.trace_sink.submit(out.release_id, out.trace)
        except Exception as e:  # Trace 때문에 턴이 실패하면 안 된다(I6)
            log.error("Trace를 outbox에 넣지 못했다 error=%s", type(e).__name__)
            self._count("trace_enqueue_failed")

    def _count(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1
