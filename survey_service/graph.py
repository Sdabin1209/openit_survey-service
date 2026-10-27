"""한 턴의 오케스트레이션 그래프(LangGraph StateGraph). 설계서 14절, D74 구조(17절).

처리 표(9-1)의 열(세션 상태)은 `load_session` 뒤의 조건부 간선, 행(이벤트 종류)은 열 노드의 표(COLUMNS)다. 단계별 실패는
`failed`에 쌓고 다음 간선이 종료 노드로 보낸다. 발화 턴은 `extract`(LoRA-A 한 번) -> `validate_extraction`(turn-engine) -> `plan_writes`
-> `commit` -> `respond`(LoRA-B)다. 검증 위반이나 503 OUTPUT_INVALID는 같은 요청을 한 번 더 부른다(`validate_extraction` -> `extract` 간선,
D75). 세션 시작은 A를 부르지 않고 B만 부른다.

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

from boundary_contracts import BoundaryError
from boundary_contracts.clients import is_output_invalid
from turn_engine import ContractViolation, validate_extraction

from . import assembly, extraction
from .errors import ReleaseUnavailable, StoreError
from .ports import LoadedRelease
from .store import effects

log = logging.getLogger("survey_service.orchestrator")

SCHEMA_CONTRACT_VERSION = 1  # TurnTrace.contract_version. 이 서비스가 지원하는 문진 스키마 정수 버전

TRACE_KEYS = ["frontend_message", "intervention_decision", "orchestrator_input", "extract_input", "extract_output", "extract_attempts",
              "extract_status", "extract_protections", "validation_ok", "validation_violations", "response_input", "response_output",
              "response_outcome", "response_protections", "frontend_input", "staff_console_input", "dispatch_input", "dispatch_output",
              "orchestrator_output", "session_before", "session_after", "log_entry", "persistence_load_request", "persistence_load_result",
              "persistence_commit_request", "persistence_commit_result"]
MAX_EXTRACT_CALLS = 2         # 첫 호출 + 같은 요청의 1회 재시도(D75)


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
    # 추출·검증·저장 계획
    extract_in: Optional[dict]
    extract_out: Optional[dict]
    extract_calls: int
    extract_next: str               # validate | extract | finalize
    violations: Optional[list]
    writes: Optional[dict]
    flow_end: bool
    resp: Optional[dict]            # 응답 계획 {kind: model_start | model | fixed | none, text?}
    alert: Optional[dict]           # 직원 알림(RiskSource가 낸 것). 입력원이 없으면 항상 None
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
        fatal=None, created=None, extract_in=None, extract_out=None, extract_calls=0, extract_next="validate", violations=None,
        writes=None, flow_end=False, resp=None, alert=None, post_doc=None, state_doc=None, release_id=None)


def _ctx(config) -> TurnCtx:
    return config["configurable"]["ctx"]


def _pause_reasons(alert: dict) -> list:
    """StaffAlert(RISK)의 사유를 일시 중지 기록(PauseReason) 모양으로."""
    out = []
    for r in alert["reasons"]:
        if r["kind"] == "RISK_SIGNAL":
            out.append({"kind": "RISK_SIGNAL", "evidence": list(r["evidence"])})
        elif r["kind"] == "RISK_RULE":
            out.append({"kind": "RISK_RULE", "target": [t["slot"] for t in r["target"]]})
    return out


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
                     "bind_release", "extract", "validate_extraction", "plan_writes", "plan_other", "commit", "deliver_staff",
                     "dispatch_result", "respond", "finalize"):
            g.add_node(name, getattr(self, "n_" + name))
        g.add_edge(START, "load_session")
        g.add_conditional_edges("load_session", self.r_column, {"fail_no_session": "fail_no_session", "col_none": "col_none",
                                                               "col_progress": "col_progress", "col_paused": "col_paused",
                                                               "col_ended": "col_ended"})
        for col in ("col_none", "col_progress", "col_paused", "col_ended"):
            g.add_conditional_edges(col, self.r_handled, {"ignore": "ignore", "bind_release": "bind_release"})
        g.add_conditional_edges("bind_release", self.r_after_bind, {"fail_no_session": "fail_no_session", "finalize": "finalize",
                                                                    "extract": "extract", "plan_other": "plan_other"})
        g.add_conditional_edges("extract", self.r_extract, {"validate_extraction": "validate_extraction", "extract": "extract",
                                                           "finalize": "finalize"})
        g.add_conditional_edges("validate_extraction", self.r_validated, {"plan_writes": "plan_writes", "extract": "extract",
                                                                         "finalize": "finalize"})
        g.add_conditional_edges("plan_writes", self.r_ok("commit"), {"commit": "commit", "finalize": "finalize"})
        g.add_conditional_edges("plan_other", self.r_ok("commit"), {"commit": "commit", "finalize": "finalize"})
        g.add_conditional_edges("commit", self.r_ok("deliver_staff"), {"deliver_staff": "deliver_staff", "finalize": "finalize"})
        g.add_conditional_edges("deliver_staff", self.r_flow, {"dispatch_result": "dispatch_result", "respond": "respond"})
        g.add_edge("dispatch_result", "respond")
        g.add_edge("respond", "finalize")
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
        return "extract" if st["route"]["full"] else "plan_other"

    @staticmethod
    def r_extract(st) -> str:
        return {"validate": "validate_extraction", "extract": "extract"}.get(st["extract_next"], "finalize")

    @staticmethod
    def r_validated(st) -> str:
        return {"plan": "plan_writes", "extract": "extract"}.get(st["extract_next"], "finalize")

    @staticmethod
    def r_ok(nxt: str):
        # 쓰기 확정 전 단계가 실패했으면(failed가 비어 있지 않다) 확정 뒤의 일 없이 종료 노드로. 저장 판단 뒤에는 되돌아가지 않는다(규칙 4)
        return lambda st: "finalize" if st["failed"] else nxt

    @staticmethod
    def r_flow(st) -> str:
        return "dispatch_result" if st["flow_end"] else "respond"

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
                doc = effects.initial_state(sid, latest.bank, self.o.flow.table.order[0])
                revision = self.o.store.create(sid, latest.release_id, doc)
                created = copy.deepcopy(doc["session_info"])
                self._timeline(sid, -1, st["turn_id"])
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
    def _fail(self, st, step: str, e: Exception, detail: str = "") -> list:
        log.warning("턴 단계 실패 session=%s turn=%s step=%s error=%s%s", st["session_id"], st["turn_id"], step, type(e).__name__,
                    f" {detail}" if detail else "")
        self.o._count(f"step_failed.{step}")
        return _step_failed(("BEFORE_COMMIT", step))

    def n_extract(self, st, config):
        """LoRA-A를 한 번 부른다(이 노드가 다시 실행되면 같은 요청의 재시도). 시작·정지 같은 비발화 턴은 이 노드에 오지 않는다."""
        ctx, doc = _ctx(config), st["state_doc"]
        calls = st["extract_calls"]
        upd: dict = {"extract_calls": calls + 1}
        tr: dict = {"extract_attempts": calls + 1}
        inp = st["extract_in"]
        if inp is None:
            try:
                inp = extraction.build_extract_input(doc, ctx.bank, self.o.flow, st["utterance"])
            except Exception as e:
                upd.update(extract_next="finalize", failed=self._fail(st, "LoRA-A", e))
                tr["extract_status"] = "ERROR"
                return {**upd, "trace": tr}
            upd["extract_in"] = inp
            tr["extract_input"] = copy.deepcopy(inp)
        try:
            res = ctx.release.session.extract(inp)
        except BoundaryError as e:
            invalid = is_output_invalid(e)
            if invalid and calls + 1 < MAX_EXTRACT_CALLS:
                upd["extract_next"] = "extract"                    # D75: 같은 요청을 1회 재시도
            else:
                upd.update(extract_next="finalize", failed=self._fail(st, "LoRA-A", e))
                tr["extract_status"] = "OUTPUT_INVALID" if invalid else "ERROR"
            return {**upd, "trace": tr}
        except Exception as e:
            upd.update(extract_next="finalize", failed=self._fail(st, "LoRA-A", e))
            tr["extract_status"] = "ERROR"
            return {**upd, "trace": tr}
        upd.update(extract_out=res.output, extract_next="validate")
        tr.update(extract_output=copy.deepcopy(res.output), extract_protections=dict(res.protections) or None)
        return {**upd, "trace": tr}

    def n_validate_extraction(self, st, config):
        """turn-engine의 validate_extraction(의미 검증)과 저장할 칸이 문항 세트에 있는지. 위반이면 같은 요청을 1회 재시도, 그래도면 턴 실패(D75)."""
        ctx, doc = _ctx(config), st["state_doc"]
        out, inp = st["extract_out"], st["extract_in"]
        try:
            viol = list(validate_extraction(inp, out, self.o.flow.table))
            viol += extraction.check_slots(out, doc["answer_records"])
        except ContractViolation as e:                       # 우리가 만든 입력이 계약을 어겼다: 재시도해도 같다
            return {"extract_next": "finalize", "failed": self._fail(st, "VALIDATE", e),
                    "trace": {"extract_status": "ERROR", "validation_ok": False, "validation_violations": None}}
        rec = [{"code": v.code, "path": v.path, "message": v.message} for v in viol]
        tr = {"validation_ok": not rec, "validation_violations": rec, "extract_output": copy.deepcopy(out)}
        if not rec:
            tr["extract_status"] = "OK"
            return {"extract_next": "plan", "violations": [], "trace": tr}
        self.o._count("validation_violation")
        self.o.count_violations(v["code"] for v in rec)
        summary = "violations=" + ",".join(f"{v['code']}@{v['path']}" for v in rec[:10]) + (f",+{len(rec) - 10}" if len(rec) > 10 else "")
        if st["extract_calls"] < MAX_EXTRACT_CALLS:
            log.info("의미 검증 위반 session=%s turn=%s attempt=%s %s", st["session_id"], st["turn_id"], st["extract_calls"], summary)   # 발화·값·메시지는 싣지 않는다
            return {"extract_next": "extract", "violations": rec, "extract_out": None, "trace": tr}
        tr["extract_status"] = "VIOLATION"
        return {"extract_next": "finalize", "violations": rec, "failed": self._fail(st, "VALIDATE", RuntimeError("violation"), summary), "trace": tr}

    def n_plan_writes(self, st, config):
        """검증을 통과한 추출 결과에서 저장(save=true)과 진행 갱신을 계획하고, 응답 계획을 정한다. 판단은 하지 않는다."""
        ctx, doc = _ctx(config), st["state_doc"]
        out = st["extract_out"]
        try:
            writes = extraction.plan_writes(doc, out)
        except Exception as e:
            return {"failed": self._fail(st, "VALIDATE", e)}
        upd: dict = {"writes": writes, "resp": {"kind": "model"}, "flow_end": out["next_item"] == "END"}
        src = self.o.risk_source
        if src is not None:                                  # 입력원이 없으면(기본) 직원 알림은 비활성이다(설계 17.6)
            try:
                alert = src.check(extract_input=st["extract_in"], extract_output=out, utterance=st["utterance"])
            except Exception as e:
                log.warning("위험 입력원 오류 session=%s error=%s", st["session_id"], type(e).__name__)
                self.o._count("risk_source_error")
                alert = None
            if alert is not None:
                upd["alert"] = alert
                if alert["kind"] == "RISK":
                    pu = writes["progress_record_update"]
                    pu["session_status"] = "PAUSED_RISK"
                    pu["pause_history_add"] = [{"kind": "PAUSE", "reasons": _pause_reasons(alert)}]
                    upd["resp"] = self._fixed(ctx, "RISK_NOTICE")
        if "alert" not in upd:
            esc = self._reask_escalation(st, ctx, out)
            if esc is not None:
                upd.update(esc)
                pu = writes["progress_record_update"]
                pu["session_status"] = "PAUSED_RISK"           # 기존 직원 호출 경로: 정지 + CALL_STAFF 알림 + 문항 세트의 고정 문장
                pu["pause_history_add"] = [{"kind": "PAUSE", "reasons": []}]
        return upd

    def _reask_escalation(self, st, ctx, out) -> Optional[dict]:
        """G12: A가 REASK·CONFIRM을 연달아 내는 횟수를 대화 로그에서 센다(세션 문서에서 계산하므로 재시작·재전송에도 같다).
        상한(SURVEY_MAX_CONSECUTIVE_REASK, 0이면 끔)을 넘는 REASK·CONFIRM이 나오면 되묻는 대신 직원을 부른다."""
        cap = self.o.max_consecutive_reask
        if cap <= 0 or out["action"] not in ("REASK", "CONFIRM"):
            return None
        n = 0
        for e in reversed(st["state_doc"]["dialogue_log"]):
            if e["turn_result"] != "COMMITTED":              # 실패·무시된 턴은 연속을 끊지 않는다
                continue
            ex = e.get("extract_output")
            if ex is None or ex["action"] not in ("REASK", "CONFIRM"):
                break                                        # 다른 action, 재개·시작 같은 턴이 연속을 끊는다
            n += 1
        if n < cap:
            return None
        self.o._count("reask_limit_reached")
        return {"alert": {"kind": "CALL_STAFF", "reasons": []}, "resp": self._fixed(ctx, "CALL_STAFF")}

    @staticmethod
    def _fixed(ctx, key: str) -> dict:
        text = ctx.bank.fixed_sentence(key) if ctx.bank else None
        return {"kind": "fixed", "text": text, "key": key}

    def n_plan_other(self, st, config):
        """발화가 아닌 턴(시작, 직원 재개·종료, 이탈): A를 부르지 않는다. 쓰기와 응답 계획만 정한다."""
        ctx, doc = _ctx(config), st["state_doc"]
        kind = st["route"]["turn_kind"]
        prog = doc["progress_record"]
        cur = prog["current_question_id"]
        pu = {"current_question_id": cur, "open_slots": copy.deepcopy(prog["open_slots"]), "skipped_add": [], "skipped_remove": [],
              "session_status": None, "pause_history_add": []}
        writes = {"answer_updates": [], "progress_record_update": None}
        upd: dict = {"writes": writes, "flow_end": False}
        if kind == "SESSION_START":
            first = self.o.flow.table.order[0]
            upd["resp"] = {"kind": "model_start"} if cur == first else {"kind": "fixed", "key": None, "text": ctx.bank.sentence(cur)}
        elif kind == "RESUME":
            pu.update(session_status="IN_PROGRESS", pause_history_add=[{"kind": "RESUME", "reasons": []}])
            writes["progress_record_update"] = pu
            upd["resp"] = self._fixed(ctx, "RESUME")
        else:                                                # INTERVENTION_END, END_LEFT
            pu["session_status"] = "ENDED_STAFF" if kind == "INTERVENTION_END" else "ENDED_LEFT"
            writes["progress_record_update"] = pu
            upd["flow_end"] = True
            upd["resp"] = self._fixed(ctx, "SURVEY_STOPPED") if kind == "INTERVENTION_END" else {"kind": "none"}
        return upd

    def n_commit(self, st, config):
        """쓰기 확정: 값 저장 + 진행 기록 갱신을 저장소의 한 번의 commit(한 트랜잭션)으로."""
        doc, writes = st["state_doc"], st["writes"]
        req = {"session_id": st["session_id"], "expected_revision": st["revision"], "writes": writes}
        res = self.o.store.commit(req, turn_id=st["turn_id"], input_hash=st["input_hash"])
        tr = {"persistence_commit_request": copy.deepcopy(req), "persistence_commit_result": copy.deepcopy(res)}
        if res["status"] == "OK":
            return {"committed": True, "revision": res["revision"], "post_doc": effects.apply_writes(doc, writes), "trace": tr}
        self.o._count("commit_failed." + res["status"])
        return {"failed": _step_failed(("BEFORE_COMMIT", "COMMIT")), "commit_unknown": res.get("write_outcome") == "UNKNOWN", "trace": tr}

    # ------------------------------------------------------------------ 노드: 쓰기 확정 뒤 (서로의 성패와 무관)
    def n_deliver_staff(self, st, config):
        sid = st["session_id"]
        upd: dict = {}
        intervention_result = st["intervention_result"]
        if st["route"]["turn_kind"] in ("RESUME", "INTERVENTION_END"):
            intervention_result = {"session_id": sid, "result": "APPLIED"}
        alert = st.get("alert")
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
        if not getattr(self.o.dispatcher, "attached", True):
            # 결과 전달 대상이 아직 없다(붙을 예정, 정상 구성). 오류가 아니라 명시된 상태로 기록한다. 전달 기록은 succeeded=false(전달되지 않았다)
            log.info("결과 전달 대상 없음 session=%s turn=%s status=NO_TARGET", st["session_id"], st["turn_id"])
            self.o._count("dispatch_no_target")
            dout = {"dispatch_record": {"succeeded": False}}
            tr["dispatch_output"] = copy.deepcopy(dout)
            self._append_dispatch(st, False)
            return {"dispatch_called": True, "trace": tr}
        try:
            dout = dict(self.o.dispatcher.dispatch(copy.deepcopy(din)))
            ok = bool(dout.get("dispatch_record", {}).get("succeeded"))
        except Exception as e:
            log.warning("결과 전달 실패 session=%s turn=%s error=%s", st["session_id"], st["turn_id"], type(e).__name__)
            dout, ok = {"dispatch_record": {"succeeded": False}}, False
        tr["dispatch_output"] = copy.deepcopy(dout)
        self._append_dispatch(st, ok)
        return {"dispatch_called": True, "trace": tr}

    def _append_dispatch(self, st, ok: bool) -> None:
        try:
            self.o.store.append_dispatch(st["session_id"], {"succeeded": ok})
        except StoreError:
            log.error("전달 기록을 남기지 못했다 session=%s turn=%s", st["session_id"], st["turn_id"])

    def n_respond(self, st, config):
        """응답 문장: 모델(LoRA-B)이 쓰는 턴은 표 찾기로 ResponseRequest를 조립해 부르고, 고정 문장 턴은 문항 세트의 고정 문장을 쓴다."""
        ctx = _ctx(config)
        plan = st["resp"]
        tr: dict = {}
        if plan["kind"] == "none":
            return {}
        if plan["kind"] == "fixed":
            if plan.get("text"):
                tr["response_outcome"] = "fixed"
                return {"response_text": plan["text"], "trace": tr}
            self.o._count("step_failed.LoRA-B")
            return {"failed": _step_failed(("AFTER_COMMIT", "LoRA-B")), "trace": tr}
        try:
            if plan["kind"] == "model_start":
                req = extraction.build_start_request(ctx.bank, self.o.tables, self.o.flow.table.order[0])
            else:
                req = extraction.build_response_request(st["extract_out"], st["extract_in"], st["post_doc"], ctx.bank, self.o.tables)
            tr["response_input"] = copy.deepcopy(req)
            res = ctx.release.session.respond(req)
        except Exception as e:
            log.warning("응답 생성 실패 session=%s turn=%s error=%s", st["session_id"], st["turn_id"], type(e).__name__)
            self.o._count("step_failed.LoRA-B")
            return {"failed": _step_failed(("AFTER_COMMIT", "LoRA-B")), "trace": tr}
        tr.update(response_output=res.output, response_outcome=res.outcome, response_protections=dict(res.protections) or None)
        return {"response_text": res.output, "trace": tr}

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
            "extract_output": copy.deepcopy(st.get("extract_out")),
            "validation_violations": copy.deepcopy(st.get("violations")),
            "staff_alert": copy.deepcopy(st.get("alert")) if committed else None,
            "response_text": response_text,
            "staff_alert_delivery": staff_delivery,
            "turn_failure_signal": signal,
            "intervention_result": intervention_result,
        }

    def _timeline(self, sid: str, index: int, turn_id: str) -> None:
        """직원 조회용 시각 기록(세션 문서에는 시각이 없다). 실패해도 턴은 계속된다."""
        tl = self.o.timeline
        if tl is None:
            return
        try:
            tl.record(sid, index, turn_id)
        except Exception as e:
            log.warning("시각 기록 실패 session=%s error=%s", sid, type(e).__name__)
            self.o._count("timeline_failed")

    def _append_log(self, st, entry: dict) -> None:
        try:
            index = len(st["state_doc"]["dialogue_log"]) if st.get("state_doc") else len(st["loaded"]["state"]["dialogue_log"])
            self.o.store.append_log(st["session_id"], entry)
            self._timeline(st["session_id"], index, st["turn_id"])
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
