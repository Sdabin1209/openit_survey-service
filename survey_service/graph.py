"""한 턴의 오케스트레이션 그래프(LangGraph StateGraph). 설계서 14절.

처리 표(9-1)의 열(세션 상태)은 `load_session` 뒤의 조건부 간선, 행(이벤트 종류)은 열 노드의 표(COLUMNS)다. 단계별 실패는
`failed`에 쌓고 다음 간선이 종료 노드로 보낸다. turn-engine의 함수는 순수 라이브러리로 그대로 두고 노드가 부른다.

- 상태(TurnState)는 JSON으로 직렬화되는 값만 가진다. 자원(저장소, 엔진, 모델 세션)은 Orchestrator가 가진다.
- 체크포인터를 쓰지 않는다(compile()에 없음). 쓰기 확정은 `commit` 노드의 저장소 한 번의 commit(한 트랜잭션)이다.
- 동작은 이전 구현(_Turn 기반)과 같다: 로그·Trace의 값과 순서, 실패 신호, 직원 콘솔 호출 순서.
"""
from __future__ import annotations

import copy
import logging
import operator
from dataclasses import asdict, dataclass
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from . import assembly
from .errors import ReleaseUnavailable, StoreError
from .ports import LoadedRelease
from .store import effects

log = logging.getLogger("survey_service.orchestrator")

SCHEMA_CONTRACT_VERSION = 1  # TurnTrace.contract_version. 이 서비스가 지원하는 문진 스키마 정수 버전

TRACE_KEYS = ["frontend_message", "intervention_decision", "orchestrator_input", "parser_input", "parser_output",
              "normalizer_input", "normalizer_output", "judge_input", "judge_output", "record_writer_input",
              "record_writer_output", "planner_input", "planner_output", "generator_input", "generator_output",
              "frontend_input", "staff_console_input", "dispatch_input", "dispatch_output", "orchestrator_output",
              "session_before", "session_after", "log_entry", "persistence_load_request", "persistence_load_result",
              "persistence_commit_request", "persistence_commit_result"]


@dataclass(frozen=True)
class Route:
    """처리 표(9-1)의 한 칸."""
    turn_kind: Optional[str]      # None이면 처리 안 함
    create: bool = False          # 세션 정보 생성
    full: bool = False            # LoRA-A~저장 판단까지 거치는 전체 흐름
    keep_log: bool = True         # 처리 안 함일 때 대화 로그를 남기는가


_IGNORE_NOLOG = Route(None, keep_log=False)
_IGNORE_LOG = Route(None, keep_log=True)

# 처리 표 9-1: 열 = 세션 상태, 행 = 입력 종류(SESSION_START | UTTERANCE | RESUME | END | SESSION_END)
COLUMNS: dict[str, dict[str, Route]] = {
    "none": {"SESSION_START": Route("SESSION_START", create=True), "UTTERANCE": Route("UTTERANCE", create=True, full=True)},
    "progress": {"SESSION_START": Route("SESSION_START"), "UTTERANCE": Route("UTTERANCE", full=True),
                 "END": Route("INTERVENTION_END"), "SESSION_END": Route("END_LEFT"),
                 "RESUME": _IGNORE_LOG},                      # 진행 중 세션에 RESUME: 뜻이 없다(G6)
    "paused": {"RESUME": Route("RESUME"), "END": Route("INTERVENTION_END"), "SESSION_END": Route("END_LEFT"),
               "SESSION_START": _IGNORE_LOG, "UTTERANCE": _IGNORE_LOG},   # 멈춘 동안의 말은 직원이 확인할 수 있게 남긴다(G5)
    "ended": {},
}
_DEFAULT = {"none": _IGNORE_NOLOG, "progress": _IGNORE_LOG, "paused": _IGNORE_LOG, "ended": _IGNORE_NOLOG}


def column_of(found: bool, status: Optional[str]) -> str:
    if not found:
        return "none"
    if status and status.startswith("ENDED"):
        return "ended"
    return "paused" if status == "PAUSED_RISK" else "progress"


def route(input_kind: str, session_found: bool, status: Optional[str]) -> Route:
    col = column_of(session_found, status)
    return COLUMNS[col].get(input_kind, _DEFAULT[col])


def _merge(a: Optional[dict], b: Optional[dict]) -> dict:
    return {**(a or {}), **(b or {})}


class TurnState(TypedDict, total=False):
    # 입력
    session_id: str
    turn_id: str
    input_hash: str
    message: Optional[dict]
    decision: Optional[dict]
    input_kind: str
    in_kind: str
    utterance: Optional[str]
    # 조회와 처리 표
    loaded: dict
    found: bool
    status: Optional[str]
    route: dict
    fatal: Optional[str]            # no_route | no_session
    # 릴리스, 세션 문서
    release_id: Optional[str]
    state_doc: Optional[dict]
    revision: Any
    created: Optional[dict]
    # 컴포넌트 출력
    parser_out: Optional[dict]
    norm_out: Optional[dict]
    judge_out: Optional[dict]
    writer_out: Optional[dict]
    records_after: Optional[list]
    plan_out: Optional[dict]
    # 쓰기 확정과 그 뒤
    committed: bool
    commit_unknown: bool
    post_doc: Optional[dict]
    failed: Annotated[list, operator.add]
    staff_status: str
    staff_delivery: str
    response_text: Optional[str]
    signal: Optional[dict]
    intervention_result: Optional[dict]
    dispatch_called: bool
    console: dict
    console_delivered: bool
    console_calls: Annotated[list, operator.add]
    trace: Annotated[dict, _merge]
    # 종료 노드의 결과
    orchestrator_output: dict
    frontend_input: Optional[dict]
    console_input: Optional[dict]
    out_release_id: Optional[str]
    cacheable: bool


class TurnCtx:
    """호출마다 노드에 건네는 자원(직렬화하지 않는다): 이 세션이 쓰는 릴리스와 문항 세트 보조 객체."""
    def __init__(self) -> None:
        self.release: Optional[LoadedRelease] = None
        self.bank: Optional[assembly.Bank] = None


def initial_state(session_id: str, turn_id: str, input_hash: str, message: Optional[dict], decision: Optional[dict]) -> TurnState:
    trace = {k: None for k in TRACE_KEYS}
    trace.update(session_id=session_id, turn_id=turn_id, contract_version=SCHEMA_CONTRACT_VERSION,
                 frontend_message=copy.deepcopy(message), intervention_decision=copy.deepcopy(decision))
    if message is not None:
        input_kind, in_kind, utterance = message["kind"], message["kind"], message.get("utterance")
    else:
        input_kind, in_kind, utterance = decision["decision"], "INTERVENTION", None
    return TurnState(
        session_id=session_id, turn_id=turn_id, input_hash=input_hash, message=copy.deepcopy(message),
        decision=copy.deepcopy(decision), input_kind=input_kind, in_kind=in_kind, utterance=utterance, trace=trace,
        failed=[], console_calls=[], committed=False, commit_unknown=False, staff_status="NONE", staff_delivery="NOT_APPLICABLE",
        response_text=None, signal=None, intervention_result=None, dispatch_called=False, console_delivered=False,
        console={"staff_alert": None, "turn_failure_signal": None, "intervention_result": None},
        fatal=None, created=None, parser_out=None, norm_out=None, judge_out=None, writer_out=None, plan_out=None,
        records_after=None, post_doc=None, state_doc=None, release_id=None)


def _ctx(config) -> TurnCtx:
    return config["configurable"]["ctx"]


def _step_failed(phase_step: tuple[str, str]) -> list:
    return [{"phase": phase_step[0], "step": phase_step[1]}]


class TurnGraph:
    """Orchestrator의 자원(저장소, 릴리스, 엔진, 접점)을 쓰는 노드 모음. build()가 컴파일한 그래프를 낸다."""

    def __init__(self, orch) -> None:
        self.o = orch

    # ------------------------------------------------------------------ 조립
    def build(self):
        g = StateGraph(TurnState)
        for name in ("load_session", "col_none", "col_progress", "col_paused", "col_ended", "ignore", "fail_no_session",
                     "bind_release", "parse", "normalize", "judge", "write_record", "plan", "commit", "deliver_staff",
                     "dispatch_result", "generate", "finalize"):
            g.add_node(name, getattr(self, "n_" + name))
        g.add_edge(START, "load_session")
        g.add_conditional_edges("load_session", self.r_column, {"fail_no_session": "fail_no_session", "col_none": "col_none",
                                                               "col_progress": "col_progress", "col_paused": "col_paused",
                                                               "col_ended": "col_ended"})
        for col in ("col_none", "col_progress", "col_paused", "col_ended"):
            g.add_conditional_edges(col, self.r_handled, {"ignore": "ignore", "bind_release": "bind_release"})
        g.add_conditional_edges("bind_release", self.r_after_bind, {"fail_no_session": "fail_no_session", "finalize": "finalize",
                                                                    "parse": "parse", "plan": "plan"})
        g.add_conditional_edges("parse", self.r_ok("normalize"), {"normalize": "normalize", "finalize": "finalize"})
        g.add_conditional_edges("normalize", self.r_ok("judge"), {"judge": "judge", "finalize": "finalize"})
        g.add_conditional_edges("judge", self.r_ok("write_record"), {"write_record": "write_record", "finalize": "finalize"})
        g.add_conditional_edges("write_record", self.r_ok("plan"), {"plan": "plan", "finalize": "finalize"})
        g.add_conditional_edges("plan", self.r_ok("commit"), {"commit": "commit", "finalize": "finalize"})
        g.add_conditional_edges("commit", self.r_ok("deliver_staff"), {"deliver_staff": "deliver_staff", "finalize": "finalize"})
        g.add_conditional_edges("deliver_staff", self.r_flow, {"dispatch_result": "dispatch_result", "generate": "generate"})
        g.add_edge("dispatch_result", "generate")
        g.add_edge("generate", "finalize")
        g.add_edge("finalize", END)
        g.add_edge("ignore", END)
        g.add_edge("fail_no_session", END)
        return g.compile()                       # 체크포인터 없음

    # ------------------------------------------------------------------ 간선
    @staticmethod
    def r_column(st) -> str:
        if st.get("fatal"):
            return "fail_no_session"
        return "col_" + column_of(st["found"], st["status"])

    @staticmethod
    def r_handled(st) -> str:
        return "ignore" if st["route"]["turn_kind"] is None else "bind_release"

    @staticmethod
    def r_after_bind(st) -> str:
        if st.get("fatal"):
            return "fail_no_session"
        if st["failed"]:
            return "finalize"
        return "parse" if st["route"]["full"] else "plan"

    @staticmethod
    def r_ok(nxt: str):
        # 쓰기 확정 전 단계가 실패했으면(failed가 비어 있지 않다) 확정 뒤의 일 없이 종료 노드로. 되돌아가지 않는다(규칙 4)
        return lambda st: "finalize" if st["failed"] else nxt

    @staticmethod
    def r_flow(st) -> str:
        return "dispatch_result" if st["plan_out"]["flow"] == "END" else "generate"

    # ------------------------------------------------------------------ 노드: 앞부분
    def n_load_session(self, st, config):
        sid = st["session_id"]
        tr = {"persistence_load_request": {"session_id": sid}}
        loaded = self.o.store.load(sid)
        tr["persistence_load_result"] = copy.deepcopy(loaded)
        if loaded["status"] == "DB_ERROR":
            return {"loaded": loaded, "trace": tr, "fatal": "no_route"}
        found = loaded["status"] == "OK"
        status = loaded["state"]["progress_record"]["session_status"] if found else None
        latest = self.o.releases.active()
        tr["orchestrator_input"] = {
            "frontend_input": copy.deepcopy(st["message"]), "intervention_decision": copy.deepcopy(st["decision"]),
            "session_found": found,
            "session_question_bank_version": loaded["state"]["session_info"]["question_bank_version"] if found else None,
            "session_status": status, "latest_question_bank_version": latest.bank["version"] if latest else 0}
        return {"loaded": loaded, "found": found, "status": status, "trace": tr}

    def _column(self, name: str):
        def node(st, config):
            r = COLUMNS[name].get(st["input_kind"], _DEFAULT[name])
            return {"route": asdict(r)}
        return node

    def n_col_none(self, st, config):
        return self._column("none")(st, config)

    def n_col_progress(self, st, config):
        return self._column("progress")(st, config)

    def n_col_paused(self, st, config):
        return self._column("paused")(st, config)

    def n_col_ended(self, st, config):
        return self._column("ended")(st, config)

    # ------------------------------------------------------------------ 노드: 처리 안 함 / 세션 없음 실패
    def n_ignore(self, st, config):
        ctx = _ctx(config)
        sid, loaded, decision = st["session_id"], st["loaded"], st["decision"]
        tr: dict = {}
        intervention_result = {"session_id": sid, "result": "IGNORED"} if decision is not None else None
        doc = loaded["state"]
        if loaded["status"] == "OK":
            tr["session_before"] = copy.deepcopy(doc)
            ctx.release = self._try_release(sid)
        cons, calls = self._console_final(st, signal=None, intervention_result=intervention_result)
        entry = None
        if st["route"]["keep_log"] and doc is not None:
            entry = self._log_entry(st, failed=[], committed=False, signal=None, intervention_result=intervention_result,
                                    response_text=None, staff_delivery="NOT_APPLICABLE",
                                    bank_version=doc["session_info"]["question_bank_version"], handled=None)
            self._append_log(st, entry)
        oo = {"turn_kind": None, "session_info_created": None, "log_entry": entry, "turn_failure_signal": None,
              "intervention_result": intervention_result, "dispatch_called": False}
        # 프론트가 보낸 말이 처리되지 않았으면 '할 말 없음'을 돌려준다(응답도 신호도 없다). 개입 결정에는 프론트가 없다.
        fi = {"response_text": None, "turn_failure_signal": None} if decision is None else None
        return self._outcome(st, ctx, oo, fi, cons, calls, tr, intervention_result=intervention_result, doc=doc)

    def _try_release(self, sid: str) -> Optional[LoadedRelease]:
        try:
            rid = self.o.store.release_id_of(sid)
            return self.o.releases.get(rid) if rid else None
        except Exception:
            return None

    def n_fail_no_session(self, st, config):
        """세션 정보 생성 실패, 조회 실패: 붙일 세션이 없어 로그를 남기지 않는다(9-1 3항)."""
        ctx = _ctx(config)
        sid, decision = st["session_id"], st["decision"]
        signal = {"session_id": sid, "write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}
        intervention_result = {"session_id": sid, "result": "FAILED"} if decision is not None else None
        cons, calls = self._console_final(st, signal=signal, intervention_result=intervention_result)
        kind = None if st.get("fatal") == "no_route" else (st["route"]["turn_kind"] if st.get("route") else None)
        oo = {"turn_kind": kind, "session_info_created": None, "log_entry": None, "turn_failure_signal": signal,
              "intervention_result": intervention_result, "dispatch_called": False}
        fi = {"response_text": None, "turn_failure_signal": signal}
        return self._outcome(st, ctx, oo, fi, cons, calls, {}, intervention_result=intervention_result, doc=None, signal=signal)

    # ------------------------------------------------------------------ 노드: 릴리스 결정, 세션 정보 생성
    def n_bind_release(self, st, config):
        ctx = _ctx(config)
        sid, loaded, found = st["session_id"], st["loaded"], st["found"]
        try:
            if found:
                rid = self.o.store.release_id_of(sid)
                ctx.release = self.o.releases.get(rid)
                doc, revision, created = loaded["state"], loaded["revision"], None
            else:
                latest = self.o.releases.active()
                if latest is None:
                    raise ReleaseUnavailable("활성 릴리스가 없다")
                ctx.release = latest
                doc = effects.initial_state(sid, latest.bank)
                revision = self.o.store.create(sid, latest.release_id, doc)
                created = copy.deepcopy(doc["session_info"])
        except ReleaseUnavailable:
            if not found:
                return {"fatal": "no_session"}
            # 세션이 묶인 릴리스를 못 쓴다: 첫 단계 실패로 처리한다(설계 5절)
            ctx.release = None
            return {"state_doc": loaded["state"], "revision": loaded["revision"], "failed": _step_failed(("BEFORE_COMMIT", "LoRA-A"))}
        except StoreError:
            return {"fatal": "no_session"}
        ctx.bank = assembly.Bank(ctx.release.bank)
        return {"state_doc": doc, "revision": revision, "created": created, "release_id": ctx.release.release_id,
                "trace": {"session_before": copy.deepcopy(doc)}}

    # ------------------------------------------------------------------ 노드: 쓰기 확정까지
    def _fail(self, st, step: str, e: Exception) -> list:
        log.warning("턴 단계 실패 session=%s turn=%s step=%s error=%s", st["session_id"], st["turn_id"], step, type(e).__name__)
        self.o._count(f"step_failed.{step}")
        return _step_failed(("BEFORE_COMMIT", step))

    def n_parse(self, st, config):
        ctx, doc = _ctx(config), st["state_doc"]
        pin = assembly.parser_input(st["utterance"], doc, ctx.bank)
        upd: dict = {"trace": {"parser_input": pin}}
        try:
            out = ctx.release.session.parse(pin)
            upd["parser_out"] = out
        except Exception as e:
            upd["parser_out"] = None
            upd["failed"] = self._fail(st, "LoRA-A", e)
        upd["trace"]["parser_output"] = copy.deepcopy(upd["parser_out"])
        return upd

    def n_normalize(self, st, config):
        ctx, doc = _ctx(config), st["state_doc"]
        nin = assembly.normalizer_input(st["parser_out"]["items"], doc, ctx.bank, len(doc["dialogue_log"]))
        upd: dict = {"trace": {"normalizer_input": nin}}
        try:
            upd["norm_out"] = self.o.engine.normalize(nin)
        except Exception as e:
            upd["norm_out"] = None
            upd["failed"] = self._fail(st, "NORMALIZE", e)
        upd["trace"]["normalizer_output"] = copy.deepcopy(upd["norm_out"])
        return upd

    def n_judge(self, st, config):
        ctx, doc = _ctx(config), st["state_doc"]
        jin = assembly.judge_input(st["parser_out"]["items"], st["norm_out"], st["utterance"], doc, ctx.bank)
        upd: dict = {"trace": {"judge_input": jin}}
        try:
            upd["judge_out"] = self.o.engine.judge(jin)
        except Exception as e:
            upd["judge_out"] = None
            upd["failed"] = self._fail(st, "JUDGE", e)
        upd["trace"]["judge_output"] = copy.deepcopy(upd["judge_out"])
        return upd

    def n_write_record(self, st, config):
        ctx, doc = _ctx(config), st["state_doc"]
        win = assembly.writer_input(st["judge_out"], doc, ctx.bank)
        upd: dict = {"trace": {"record_writer_input": win}}
        try:
            res = self.o.engine.write_record(win)
            records_after = effects.apply_store_result(doc["answer_records"], res)
        except Exception as e:
            upd["failed"] = self._fail(st, "RECORD_WRITE", e)
            return upd
        upd.update(writer_out=res, records_after=records_after)
        upd["trace"]["record_writer_output"] = copy.deepcopy(res)
        return upd

    def n_plan(self, st, config):
        ctx, doc = _ctx(config), st["state_doc"]
        full = st["route"]["full"]
        store_result, judgments, utter_judgment, store_summary = effects.empty_store_result(), [], None, None
        records_after = doc["answer_records"]
        if full:
            store_result, judgments = st["writer_out"], st["judge_out"]["judgments"]
            utter_judgment, store_summary = st["judge_out"]["utterance_judgment"], st["writer_out"]["store_summary"]
            records_after = st["records_after"]
        pl_in = assembly.planner_input(st["route"]["turn_kind"], utter_judgment, judgments, store_summary, records_after, doc, ctx.bank)
        upd: dict = {"trace": {"planner_input": pl_in}}
        try:
            upd["plan_out"] = self.o.engine.plan(pl_in)
        except Exception as e:
            upd["plan_out"] = None
            upd["failed"] = self._fail(st, "PLAN", e)
        upd["trace"]["planner_output"] = copy.deepcopy(upd["plan_out"])
        return upd

    def n_commit(self, st, config):
        """쓰기 확정: 저장 결정 + 진행 기록 갱신 + Plan 기록 갱신을 저장소의 한 번의 commit(한 트랜잭션)으로."""
        doc, po = st["state_doc"], st["plan_out"]
        store_result = st["writer_out"] if st["route"]["full"] else effects.empty_store_result()
        writes = {"store_result": store_result, "progress_record_update": po["progress_record_update"],
                  "planner_record_update": po["planner_record_update"]}
        req = {"session_id": st["session_id"], "expected_revision": st["revision"], "writes": writes}
        res = self.o.store.commit(req, turn_id=st["turn_id"], input_hash=st["input_hash"])
        tr = {"persistence_commit_request": copy.deepcopy(req), "persistence_commit_result": copy.deepcopy(res)}
        if res["status"] == "OK":
            return {"committed": True, "revision": res["revision"], "post_doc": effects.apply_writes(doc, writes), "trace": tr}
        self.o._count("commit_failed." + res["status"])
        return {"failed": _step_failed(("BEFORE_COMMIT", "COMMIT")), "commit_unknown": res.get("write_outcome") == "UNKNOWN", "trace": tr}

    # ------------------------------------------------------------------ 노드: 쓰기 확정 뒤 (서로의 성패와 무관)
    def n_deliver_staff(self, st, config):
        sid, po = st["session_id"], st["plan_out"]
        upd: dict = {}
        intervention_result = st["intervention_result"]
        if st["route"]["turn_kind"] in ("RESUME", "INTERVENTION_END"):
            intervention_result = {"session_id": sid, "result": "APPLIED"}
        alert = po.get("staff_alert")
        console = copy.deepcopy(st["console"])
        # 직원 알림은 지연 없이: 응답 생성보다 앞에서 한다
        if alert is not None:
            console["staff_alert"] = {"session_id": sid, "staff_alert": copy.deepcopy(alert)}
        console["intervention_result"] = intervention_result
        upd.update(intervention_result=intervention_result, console=console)
        if alert is not None or intervention_result is not None:
            upd["console_calls"] = [copy.deepcopy(console)]
            try:
                self.o.staff_console.deliver(copy.deepcopy(console))
                upd["console_delivered"] = True
                if alert is not None:
                    upd.update(staff_status="DELIVERED", staff_delivery="SUCCESS")
            except Exception as e:
                log.warning("직원 콘솔 전달 실패 session=%s turn=%s error=%s", sid, st["turn_id"], type(e).__name__)
                self.o._count("step_failed.DELIVER_STAFF")
                upd["failed"] = _step_failed(("AFTER_COMMIT", "DELIVER_STAFF"))
                if alert is not None:
                    upd.update(staff_status="DELIVERY_FAILED", staff_delivery="FAILURE")
                    console["staff_alert"] = None
        return upd

    def n_dispatch_result(self, st, config):
        """결과 전달(문진이 끝나는 턴)."""
        din = assembly.dispatch_input(st["post_doc"])
        tr = {"dispatch_input": din}
        try:
            dout = dict(self.o.dispatcher.dispatch(copy.deepcopy(din)))
            ok = bool(dout.get("dispatch_record", {}).get("succeeded"))
        except Exception as e:
            log.warning("결과 전달 실패 session=%s turn=%s error=%s", st["session_id"], st["turn_id"], type(e).__name__)
            dout, ok = {"dispatch_record": {"succeeded": False}}, False
        tr["dispatch_output"] = copy.deepcopy(dout)
        try:
            self.o.store.append_dispatch(st["session_id"], {"succeeded": ok})
        except StoreError:
            log.error("전달 기록을 남기지 못했다 session=%s turn=%s", st["session_id"], st["turn_id"])
        return {"dispatch_called": True, "trace": tr}

    def n_generate(self, st, config):
        ctx = _ctx(config)
        gin = assembly.generator_input(st["plan_out"]["directive"], st["utterance"])
        tr = {"generator_input": gin}
        try:
            gen = ctx.release.session.generate(gin)
            tr["generator_output"] = copy.deepcopy(gen)
            return {"response_text": gen["response_text"], "trace": tr}
        except Exception as e:
            log.warning("응답 생성 실패 session=%s turn=%s error=%s", st["session_id"], st["turn_id"], type(e).__name__)
            self.o._count("step_failed.LoRA-B")
            return {"failed": _step_failed(("AFTER_COMMIT", "LoRA-B")), "trace": tr}

    # ------------------------------------------------------------------ 종료 노드: 로그, 신호, 프론트
    @staticmethod
    def _signal(sid: str, failed: list, committed: bool, staff_status: str) -> Optional[dict]:
        if not failed:
            return None
        return {"session_id": sid, "write_committed": "COMMITTED" if committed else "NOT_COMMITTED", "staff_alert": staff_status}

    def n_finalize(self, st, config):
        ctx = _ctx(config)
        sid, decision, committed = st["session_id"], st["decision"], st["committed"]
        failed = list(st["failed"])
        intervention_result = st["intervention_result"]
        if decision is not None and not committed:
            intervention_result = {"session_id": sid, "result": "FAILED"}
        # 프론트 전달(O06 대기: 자리만 있다)
        signal = self._signal(sid, failed, committed, st["staff_status"])
        fi_text = st["response_text"]
        sink = self.o.frontend_sink
        if committed and sink is not None and st["response_text"] is not None:
            try:
                sink.deliver({"response_text": st["response_text"], "turn_failure_signal": signal})
            except Exception as e:
                log.warning("프론트 전달 실패 session=%s turn=%s error=%s", sid, st["turn_id"], type(e).__name__)
                self.o._count("step_failed.DELIVER_FRONT")
                failed.append({"phase": "AFTER_COMMIT", "step": "DELIVER_FRONT"})
                signal = self._signal(sid, failed, committed, st["staff_status"])
                fi_text = None
        fi = {"response_text": fi_text, "turn_failure_signal": signal}
        cons, calls = self._console_final(st, signal=signal, intervention_result=intervention_result)
        doc = st["state_doc"]
        bank_version = ctx.release.bank["version"] if ctx.release else doc["session_info"]["question_bank_version"]
        entry = self._log_entry(st, failed=failed, committed=committed, signal=signal, intervention_result=intervention_result,
                                response_text=st["response_text"], staff_delivery=st["staff_delivery"], bank_version=bank_version,
                                handled=st["route"]["turn_kind"])
        self._append_log(st, entry)
        oo = {"turn_kind": st["route"]["turn_kind"], "session_info_created": st["created"], "log_entry": entry,
              "turn_failure_signal": signal, "intervention_result": intervention_result, "dispatch_called": st["dispatch_called"]}
        return self._outcome(st, ctx, oo, fi, cons, calls, {"frontend_input": copy.deepcopy(fi)},
                             intervention_result=intervention_result, doc=doc, signal=signal, failed_add=failed[len(st["failed"]):])

    def _console_final(self, st, *, signal, intervention_result):
        """직원 콘솔에 아직 못 나간 실패 신호·개입 결과를 한 번 더 보낸다(이미 나간 알림은 되풀이하지 않는다).
        마지막으로 보낸 내용이 이 턴의 StaffConsoleInput(출력, Trace)이다. 돌려주는 값: (최종 console, 추가된 호출 목록)."""
        console = st["console"]
        delivered = st["console_delivered"]
        payload = {"staff_alert": None if delivered else console["staff_alert"], "turn_failure_signal": signal,
                   "intervention_result": None if delivered else intervention_result}
        if payload["turn_failure_signal"] is None and payload["intervention_result"] is None:
            return console, []
        try:
            self.o.staff_console.deliver(copy.deepcopy(payload))
        except Exception as e:
            log.warning("직원 콘솔 신호 전달 실패 session=%s turn=%s error=%s", st["session_id"], st["turn_id"], type(e).__name__)
        return payload, [copy.deepcopy(payload)]

    def _log_entry(self, st, *, failed, committed, signal, intervention_result, response_text, staff_delivery, bank_version, handled) -> dict:
        po = st.get("plan_out")
        if failed:
            result: Any = {"failed_steps": copy.deepcopy(failed)}
        elif handled is None:
            result = "NOT_APPLICABLE"
        else:
            result = "COMMITTED"
        decision = st["decision"]
        return {
            "input": {"kind": st["in_kind"], "utterance": st["utterance"], "intervention_decision": decision["decision"] if decision else None},
            "handling": {"handled": handled} if handled else "IGNORED",
            "question_bank_version": bank_version,
            "turn_result": result,
            "parser_output": copy.deepcopy(st.get("parser_out")), "normalizer_output": copy.deepcopy(st.get("norm_out")),
            "judge_output": copy.deepcopy(st.get("judge_out")), "record_writer_output": copy.deepcopy(st.get("writer_out")),
            "directive": copy.deepcopy(po["directive"]) if po else None,
            "flow": po["flow"] if po else None,
            "progress_record_update": copy.deepcopy(po["progress_record_update"]) if po else None,
            "staff_alert": copy.deepcopy(po["staff_alert"]) if po and committed else None,
            "response_text": response_text,
            "staff_alert_delivery": staff_delivery,
            "turn_failure_signal": signal,
            "intervention_result": intervention_result,
        }

    def _append_log(self, st, entry: dict) -> None:
        try:
            self.o.store.append_log(st["session_id"], entry)
        except StoreError:
            log.error("대화 로그를 남기지 못했다 session=%s turn=%s", st["session_id"], st["turn_id"])
            self.o._count("log_append_failed")

    def _outcome(self, st, ctx: TurnCtx, oo: dict, fi: Optional[dict], cons: dict, calls: list, trace_add: dict, *,
                 intervention_result, doc, signal=None, failed_add=None) -> dict:
        """종료 노드가 돌려주는 결과와 Trace의 마무리 칸."""
        sc = cons if any(v is not None for v in cons.values()) else None
        tr = dict(trace_add)
        tr["orchestrator_output"] = copy.deepcopy(oo)
        tr["log_entry"] = copy.deepcopy(oo["log_entry"])
        tr["staff_console_input"] = copy.deepcopy(sc)
        if fi is not None and st["trace"].get("frontend_input") is None and tr.get("frontend_input") is None:
            tr["frontend_input"] = copy.deepcopy(fi)
        after = self.o.store.load(st["session_id"])
        if after["status"] == "OK":
            tr["session_after"] = after["state"]
        if doc is not None:
            tr["turn_index"] = len(doc["dialogue_log"])
        sid = st["session_id"]
        rid = ctx.release.release_id if ctx.release else (self.o.store.release_id_of(sid) or self.o._active_id())
        upd = {"orchestrator_output": oo, "frontend_input": fi, "console_input": copy.deepcopy(sc), "out_release_id": rid,
               "cacheable": not st.get("commit_unknown", False), "trace": tr, "console_calls": calls,
               "intervention_result": intervention_result, "signal": signal if signal is not None else oo.get("turn_failure_signal")}
        if failed_add:
            upd["failed"] = failed_add
        return upd
