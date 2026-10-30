"""Orchestrator: 한 턴을 처리한다(docs/rules/orchestrator.md, preconditions.md, timing.md).

한 턴의 분기와 단계는 LangGraph 그래프(survey_service/graph.py, 설계서 14절)가 맡는다. 이 모듈은 그래프 밖에서 유지하는 것을 한다:
세션별 직렬화, 입력 검증, turn_id 멱등(저장된 결과 재생), 결과 저장, Trace outbox로 넘기기.
판단은 하지 않는다(추출은 LoRA-A, 출력의 의미 규칙은 서버가 보증한다(ADR 0010)). 공개 API(I10)는 survey_service/api.py가 이 handle_turn을 부른다.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Mapping, Optional

from boundary_contracts import canonical

from .. import types
from types import SimpleNamespace

from .errors import SessionEnded, SessionNotFound, StaffCallRejected, StoreError, TurnIdConflict, TurnInProgress
from .turn_graph import SCHEMA_CONTRACT_VERSION, TRACE_KEYS, TurnCtx, TurnGraph, initial_state, _pause_reasons  # noqa: F401 (재노출)
from ..components.turn_route import Route, route  # noqa: F401 (재노출)
import survey_schema

from ..components.flow_table import Flow, load_flow
from . import checked
from .ports import (FrontendSink, ReleaseProvider, ResultDispatcher, RiskSource, StaffConsole, TraceSink, UnattachedDispatcher,
                    UnconfiguredStaffConsole)
from ..components.response_request import ResponseTables
from ..components import session_store
from ..components.session_store import SessionStorePort

log = logging.getLogger("survey_service.orchestrator")

IN_KINDS = {"SESSION_START": "SESSION_START", "UTTERANCE": "UTTERANCE", "SESSION_END": "SESSION_END"}


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


class Orchestrator:
    def __init__(self, store: SessionStorePort, releases: ReleaseProvider, *, flow: Optional[Flow] = None,
                 tables: Optional[ResponseTables] = None, risk_source: Optional[RiskSource] = None,
                 staff_console: Optional[StaffConsole] = None, dispatcher: Optional[ResultDispatcher] = None,
                 frontend_sink: Optional[FrontendSink] = None, trace_sink: Optional[TraceSink] = None,
                 max_consecutive_reask: int = 0, timeline=None, turn_deadline_seconds: float = 0.0, extract_input_max_chars: int = 0) -> None:
        self.store = store
        self.releases = releases
        self.flow = flow or load_flow()                                  # 문진 스키마의 문항 순서·건너뛰기 표(D76)
        self.tables = tables or ResponseTables(survey_schema.load_data("response_tables"))   # 미확정 초안 표
        self.risk_source = risk_source                                   # None이면 직원 알림 입력원이 없다(비활성)
        self.staff_console = staff_console or UnconfiguredStaffConsole()
        self.dispatcher = dispatcher or UnattachedDispatcher()
        self.frontend_sink = frontend_sink
        self.max_consecutive_reask = max(0, int(max_consecutive_reask))   # 0이면 끔(G12). 서비스 기본은 3(settings.py, ADR-0020)
        self.trace_sink = trace_sink
        self.timeline = timeline        # 선택. record(session_id, turn_index, turn_id)를 가진 객체(AlertStore). 직원 조회의 시각용, 실패해도 턴은 계속된다
        self.turn_deadline_seconds = max(0.0, float(turn_deadline_seconds))      # 0이면 마감 없음(서비스는 설정 기본 6초를 준다)
        self.extract_input_max_chars = max(0, int(extract_input_max_chars))      # 0이면 줄이지 않는다(서비스는 설정 기본 2400을 준다)
        self._locks = _SessionLocks()
        self.errors = SimpleNamespace(StoreError=StoreError, TurnIdConflict=TurnIdConflict, TurnInProgress=TurnInProgress, SessionNotFound=SessionNotFound,
                                      SessionEnded=SessionEnded, StaffCallRejected=StaffCallRejected)      # link_public_api가 오류 종류를 알아보는 데 쓴다(컴포넌트가 wiring을 import하지 않으므로 건넨다)
        self.blank_details = checked.blank_details
        self._inflight: set = set()                # 처리 중인 (세션, 턴 ID). 같은 턴이 겹치면 두 번째는 새로 시작하지 않는다
        self._inflight_guard = threading.Lock()
        self._stats_lock = threading.Lock()
        self.counters: dict[str, int] = {}
        self.violations: dict[str, int] = {}      # 의미 검증 위반 코드별 건수(값 없음)
        self._graph = TurnGraph(self).build()      # 체크포인터 없이 컴파일

    # ------------------------------------------------------------------ 공개
    def handle_turn(self, *, turn_id: str, frontend_message: Optional[Mapping] = None,
                    intervention_decision: Optional[Mapping] = None, staff_id: Optional[str] = None,
                    started: Optional[float] = None) -> TurnOutcome:
        """한 턴을 처리한다. 같은 (세션, 턴 ID)는 저장한 결과를 그대로 돌려준다(이중 저장 없음).

        frontend_message(FrontendMessage)와 intervention_decision(InterventionDecision) 중 정확히 하나를 준다.
        입력이 계약을 어기면 ValueError(턴 실패가 아니라 호출 오류).
        started: 요청을 받은 시각(time.monotonic()). 주면 총 마감을 그 시각부터 센다(API의 사전 확인이 쓴 시간도 마감에 든다).
        """
        # 입력 검사는 스키마 하나다(DESIGN 5절): 어기면 SchemaViolation(ValueError)
        types.validate("TurnRequest", {"turn_id": turn_id, "frontend_message": frontend_message, "intervention_decision": intervention_decision})
        sid = (frontend_message if frontend_message is not None else intervention_decision)["session_id"]
        input_hash = canonical.sha256_hash({"m": frontend_message, "d": intervention_decision})
        key = (sid, turn_id)
        with self._inflight_guard:
            if key in self._inflight:
                self._count("turn_in_progress")
                raise TurnInProgress(f"같은 턴 ID가 처리 중이다 (turn={turn_id})")
            self._inflight.add(key)
        try:
            ctx = TurnCtx()
            if self.turn_deadline_seconds <= 0:
                return self._handle_locked(sid, turn_id, frontend_message, intervention_decision, input_hash, staff_id, ctx)
            # 턴 전체(세션 락 대기, 멱등 확인, 모델 호출 재시도, DB 대기, 쓰기 확정)에 총 마감을 건다(FAULT_POLICY 3).
            # 처리는 데몬 스레드에서 돌고, 마감을 넘기면 ctx.cancelled로 표시해 늦게 끝난 작업이 확정(commit)하지 않게 한다.
            ctx.deadline = (started if started is not None else time.monotonic()) + self.turn_deadline_seconds
            box: dict = {}

            def work() -> None:
                try:
                    box["out"] = self._handle_locked(sid, turn_id, frontend_message, intervention_decision, input_hash, staff_id, ctx)
                except BaseException as e:        # noqa: BLE001 호출자 스레드에서 다시 올린다
                    box["err"] = e
            t = threading.Thread(target=work, name=f"turn-{turn_id}", daemon=True)
            t.start()
            t.join(max(0.05, ctx.deadline - time.monotonic()))
            if t.is_alive():
                ctx.cancelled.set()
                self._count("turn_deadline_exceeded")
                log.warning("턴 총 마감 초과 session=%s turn=%s deadline=%ss", sid, turn_id, self.turn_deadline_seconds)
                return self._deadline_outcome(sid)
            if "err" in box:
                raise box["err"]
            return box["out"]
        finally:
            with self._inflight_guard:
                self._inflight.discard(key)

    def _handle_locked(self, sid, turn_id, frontend_message, intervention_decision, input_hash, staff_id, ctx) -> TurnOutcome:
        with self._locks.hold(sid):
            replay = self._replay(sid, turn_id, input_hash)
            if replay is not None:
                return replay
            out = self._process(sid, turn_id, frontend_message, intervention_decision, input_hash, staff_id, ctx)
            result = None
            try:
                sig = (out.orchestrator_output or {}).get("turn_failure_signal") or {}
                # 확정된 턴만 멱등 재생한다(CR-D25): 확정되지 못한 턴(NOT_COMMITTED: 모델 단계 실패 등)은 결과를 고정하지 않아 같은 turn_id 재전송이 새로 처리된다
                if out.cacheable and sig.get("write_committed") != "NOT_COMMITTED" and not ctx.cancelled.is_set():      # 마감으로 끝낸 턴의 늦은 결과는 고정하지 않는다(같은 turn_id 재전송이 새로 처리된다)
                    result = {"orchestrator_output": out.orchestrator_output, "frontend_input": out.frontend_input,
                              "staff_console_input": out.staff_console_input, "release_id": out.release_id}
                envelope = self.trace_envelope(out.release_id, out.trace)
                if envelope is not None:          # 같은 DB: 멱등 결과와 완성된 Trace 행을 한 트랜잭션으로 (H02, ADR-0023)
                    self.store.finalize_turn(sid, turn_id, input_hash, result, envelope)
                    return out
                if result is not None:
                    self.store.save_turn_result(sid, turn_id, input_hash, result)
            except StoreError:
                log.error("턴 결과를 멱등 기록에 남기지 못했다 (session=%s turn=%s)", sid, turn_id)
            self._submit_trace(out)
            return out

    # ------------------------------------------------------------------ 외부 위험 신호 (D절)
    def apply_risk_signal(self, sid: str, signal: Mapping, *, alerts, signals) -> tuple[bool, dict]:
        """외부 감지기가 보낸 위험 신호를 기존 위험 처리와 같은 효과로 반영한다: 세션을 PAUSED_RISK로 멈추고(정지 기록에 신호 출처·코드)
        직원 알림을 만든다(rule_id = `source:code`). 환자 발화는 다루지 않는다. 같은 signal_id는 저장된 결과를 돌려준다.
        돌려주는 값: (새로 처리했는가, {alert_id, session_status}). 세션이 없으면 SessionNotFound, 끝났으면 SessionEnded, 저장 실패는 StoreError.
        alerts는 raise_alert(session_id, kind, rule_id)를, signals는 get_signal·record_signal을 가진 객체(AlertService, AlertStore)다."""
        from ..components.risk_screen import risk_alert
        severity = signal.get("severity", "RISK")
        with self._locks.hold(sid):
            prev = signals.get_signal(sid, signal["signal_id"])
            if prev is not None:
                return False, prev
            loaded = checked.store_load(self.store, sid)
            if loaded["status"] == "DB_ERROR":
                raise session_store.StoreError("세션을 읽을 수 없다")
            if loaded["status"] != "OK":
                raise SessionNotFound(sid)
            status = loaded["state"]["progress_record"]["session_status"]
            if status.startswith("ENDED"):
                raise SessionEnded(sid)
            alert = risk_alert(signal["source"], signal["code"], severity)
            if status != "PAUSED_RISK":
                prog = loaded["state"]["progress_record"]
                writes = {"answer_updates": [], "progress_record_update": {
                    "current_question_id": prog["current_question_id"], "open_slots": copy.deepcopy(prog["open_slots"]), "skipped_add": [],
                    "skipped_remove": [], "session_status": "PAUSED_RISK",
                    "pause_history_add": [{"kind": "PAUSE", "reasons": _pause_reasons(alert) if severity == "RISK" else [],
                                           "pause_reason": "RISK" if severity == "RISK" else "OTHER"}]}}
                res = checked.store_commit(self.store, {"session_id": sid, "expected_revision": loaded["revision"], "writes": writes},
                                        turn_id="risk-signal:" + signal["signal_id"], input_hash=canonical.sha256_hash({"risk_signal": dict(signal)}))
                if res["status"] != "OK":
                    raise session_store.StoreError("세션을 멈추지 못했다")
                status = "PAUSED_RISK"
            rule_id = f"{signal['source']}:{signal['code']}"
            try:
                a = alerts.raise_alert(sid, severity, rule_id, "RISK" if severity == "RISK" else "OTHER")      # 외부 감지기의 CALL_STAFF는 위험 판정이 아니라 호출이다: OTHER (CR-H002)
            except Exception as e:                         # 전달 경로가 없어도 알림은 저장돼 있다(DeliveryFailed.alert)
                a = getattr(e, "alert", None)
                if a is None:
                    raise
                log.warning("위험 신호 알림이 닿은 전달 경로 없음 session=%s", sid)
                self._count("risk_signal_delivery_failed")
            signals.record_signal(sid, signal, int(a["alert_id"][3:]), status)
            self._count("risk_signal_applied")
            return True, {"alert_id": a["alert_id"], "session_status": status}

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

    # ------------------------------------------------------------------ 처리 (그래프)
    def _process(self, sid: str, turn_id: str, message: Optional[Mapping], decision: Optional[Mapping], input_hash: str,
                 staff_id: Optional[str], ctx: TurnCtx) -> TurnOutcome:
        st = initial_state(sid, turn_id, input_hash, dict(message) if message is not None else None,
                           dict(decision) if decision is not None else None, staff_id)
        final = self._graph.invoke(st, config={"configurable": {"ctx": ctx}})
        return TurnOutcome(final["orchestrator_output"], final["frontend_input"], final["console_input"], final["trace"],
                           final["out_release_id"], cacheable=final["cacheable"])

    def _active_id(self) -> Optional[str]:
        a = self.releases.active()
        return a.release_id if a else None

    @staticmethod
    def _deadline_outcome(sid: str) -> TurnOutcome:
        """마감을 넘긴 턴: 확정하지 않았다고 알린다(NOT_COMMITTED). 결과를 고정하지 않는다(같은 turn_id 재전송이 안전하다).
        마감 직전에 시작한 쓰기가 뒤늦게 반영됐다면 재전송은 기존 멱등 경로(쓰기는 확정, 결과 없음)로 그 사실을 알린다."""
        signal = {"session_id": sid, "write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}
        oo = {"turn_kind": None, "session_info_created": None, "log_entry": None, "turn_failure_signal": signal,
              "intervention_result": None, "dispatch_called": False}
        return TurnOutcome(oo, {"response_text": None, "turn_failure_signal": signal}, None, trace={}, release_id=None, cacheable=False)

    def atomic_trace(self) -> bool:
        """Trace outbox가 세션 저장소와 같은 DB에 있어 세션 commit과 같은 트랜잭션에 쓸 수 있는가(트랜잭셔널 outbox)."""
        gate = getattr(self.store, "outbox_gate", None)
        return (gate is not None and getattr(self.store, "supports_trace_outbox", False)
                and getattr(self.trace_sink, "outbox", None) is gate and hasattr(self.trace_sink, "envelope"))

    def trace_envelope(self, release_id, trace) -> Optional[dict]:
        """같은 DB 모드일 때 TraceEnvelope. 아니면 None(기존 경로: _submit_trace)."""
        if not self.atomic_trace() or release_id is None or not trace:
            return None
        return self.trace_sink.envelope(release_id, trace)

    def _submit_trace(self, out: TurnOutcome) -> None:
        if self.trace_sink is None or out.release_id is None:
            return
        try:
            self.trace_sink.submit(out.release_id, out.trace)
        except Exception as e:  # Trace 때문에 턴이 실패하면 안 된다(I6)
            log.error("Trace를 outbox에 넣지 못했다 error=%s", type(e).__name__)
            self._count("trace_enqueue_failed")

    def _count(self, name: str) -> None:
        with self._stats_lock:
            self.counters[name] = self.counters.get(name, 0) + 1

    def count_violations(self, codes) -> None:
        with self._stats_lock:
            for c in codes:
                self.violations[c] = self.violations.get(c, 0) + 1

    def violation_stats(self) -> dict:
        """/readyz용. 검증을 한 번 돌릴 때 나온 위반마다 코드별로 센다(같은 턴의 재시도도 각각 센다)."""
        with self._stats_lock:
            by = dict(self.violations)
            turns = self.counters.get("validation_violation", 0)
        return {"validations_with_violation": turns, "total": sum(by.values()), "by_code": by}

    def dispatcher_status(self) -> dict:
        """결과 전달 대상. 붙어 있지 않은 것은 오류가 아니라 명시된 상태다(COMPONENTS.yaml: 붙을 예정)."""
        if getattr(self.dispatcher, "attached", True):
            return {"attached": True}
        with self._stats_lock:
            n = self.counters.get("dispatch_no_target", 0)
        return {"attached": False, "status": "NO_TARGET", "detail": "결과 전달 대상 없음(붙을 예정). 전달 기록은 succeeded=false로 남는다", "skipped_turns": n}
