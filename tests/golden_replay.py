"""골든 예시 대조: 컴포넌트 출력을 재생하는 엔진·모델 대역으로 Orchestrator 자체를 골든과 비교한다.

골든에는 문항 세트와 모델·엔진 입력이 없고 로그 항목 안에 컴포넌트 출력이 있다(CR-002). 그래서 비교하는 것은 Orchestrator의
입력 조립 결과(OrchestratorInput), 처리 표, 저장 반영(최종 상태), 진행 기록, 로그 항목, 실패 처리, 직원 알림, 결과 전달이다.
turn-engine과 모델의 판단은 비교 대상이 아니다.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

from survey_service.orchestrator import Orchestrator
from survey_service.ports import Engine, LoadedRelease
from survey_service.release.registry import ReleaseRegistry
from survey_service.store.memory import MemorySessionStore

from .fakes import FaultyStore, RecordingConsole, RecordingDispatcher, RecordingSink
from .skeleton_bank import skeleton_bank

GOLDEN = Path(__file__).parent / "golden"
SID = "S-TEST"


def load_cases() -> dict[str, dict]:
    out = {}
    for f in sorted(GOLDEN.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        turns: dict[int, dict] = {}
        for s in d["specimens"]:
            turns.setdefault(s["turn"], {})[(s["contract"], s["role"])] = s["data"]
        out[d["case"]] = turns
    return out


class Boom(RuntimeError):
    pass


class ReplaySession:
    """모델 세션 대역: 로그 항목의 parser_output과 골든의 응답 문장을 돌려준다."""
    def __init__(self, parser_output, response_text, fail_parse: bool, fail_generate: bool) -> None:
        self.parser_output, self.response_text = parser_output, response_text
        self.fail_parse, self.fail_generate = fail_parse, fail_generate

    def parse(self, input):
        if self.fail_parse:
            raise Boom()
        return copy.deepcopy(self.parser_output)

    def generate(self, input):
        if self.fail_generate:
            raise Boom()
        return {"response_text": self.response_text, "is_fallback": False}


def replay_engine(log: dict, planner_record: Optional[dict], fail: set[str]) -> Engine:
    def stage(name, value):
        def fn(_input):
            if name in fail:
                raise Boom()
            return copy.deepcopy(value)
        return fn

    plan_out = None
    if log.get("directive") is not None:
        plan_out = {"directive": log["directive"], "flow": log["flow"], "staff_alert": log.get("staff_alert"),
                    "progress_record_update": log["progress_record_update"], "planner_record_update": planner_record}
    return Engine(normalize=stage("NORMALIZE", log.get("normalizer_output")), judge=stage("JUDGE", log.get("judge_output")),
                  write_record=stage("RECORD_WRITE", log.get("record_writer_output")), plan=stage("PLAN", plan_out))


def injected_failures(log: dict, oi: dict, signal) -> tuple[set[str], bool]:
    """골든의 기대 로그에서 어떤 단계를 실패시킬지 읽는다. (실패 단계 집합, 세션 정보 생성 실패 여부)"""
    if log:
        tr = log.get("turn_result")
        steps = {fs["step"] for fs in tr["failed_steps"]} if isinstance(tr, dict) else set()
        return steps, False
    create_fail = bool(signal) and not oi["session_found"]
    return set(), create_fail


def run_turn(case: str, n: int, turn: dict) -> dict:
    """한 턴을 새 저장소에서 돌리고 비교에 쓸 값을 모은다. 키는 '계약/역할'."""
    msg = turn.get(("FrontendMessage", "input"))
    dec = turn.get(("InterventionDecision", "input"))
    oi = turn[("OrchestratorInput", "input")]
    expected_oo = turn[("OrchestratorOutput", "output")] if ("OrchestratorOutput", "output") in turn else turn.get(("OrchestratorOutput", "representation"))
    log = (expected_oo or {}).get("log_entry") or {}
    fi_exp = turn.get(("FrontendInput", "output"))
    signal = (expected_oo or {}).get("turn_failure_signal")
    fail_steps, create_fail = injected_failures(log, oi, signal)
    final = turn.get(("SessionStore", "final_state"))
    latest_bank = skeleton_bank(oi["latest_question_bank_version"] or 1)
    session_bank = skeleton_bank(oi["session_question_bank_version"] or 1)
    response_text = log.get("response_text")
    if response_text is None and fi_exp:
        response_text = fi_exp.get("response_text")
    session = ReplaySession(log.get("parser_output"), response_text, "LoRA-A" in fail_steps, "LoRA-B" in fail_steps)
    reg = ReleaseRegistry()
    if oi["session_found"] and session_bank["version"] != latest_bank["version"]:
        # 진행 중인 세션은 자기 릴리스로 끝까지 처리한다(새 세션부터 새 릴리스, O13)
        reg.register(LoadedRelease("rel-golden-old", {}, session_bank, session))
    reg.register(LoadedRelease("rel-golden-new", {}, latest_bank, session), activate=True)
    mem = MemorySessionStore()
    if oi["session_found"]:
        mem.create(SID, "rel-golden-old" if session_bank["version"] != latest_bank["version"] else "rel-golden-new",
                   copy.deepcopy(turn[("SessionStore", "initial_state")]))
    store = FaultyStore(mem, fail_create=create_fail, fail_commit="COMMIT" if "COMMIT" in fail_steps else None)
    console = RecordingConsole(fail="DELIVER_STAFF" in fail_steps)
    disp_out = turn.get(("DispatchOutput", "output"))
    dispatcher = RecordingDispatcher(output=disp_out)
    sink = RecordingSink(fail="DELIVER_FRONT" in fail_steps) if "DELIVER_FRONT" in fail_steps else None
    planner_record = final["planner_record"] if final else None
    orch = Orchestrator(store, reg, engine=replay_engine(log, planner_record, fail_steps), staff_console=console,
                        dispatcher=dispatcher, frontend_sink=sink)
    out = orch.handle_turn(turn_id=f"{case}-t{n}", frontend_message=msg, intervention_decision=dec)
    got_final = mem.load(SID)["state"]
    t = out.trace
    got = {
        "OrchestratorInput/input": t.get("orchestrator_input"),
        "OrchestratorOutput/output": out.orchestrator_output,
        "FrontendInput/output": out.frontend_input,
        "SessionStore/final_state": got_final,
        "SessionStore/processing_state": t.get("session_before"),
        "StaffConsoleInput/output": out.staff_console_input,
        "DispatchInput/output": t.get("dispatch_input"),
        "DispatchOutput/output": t.get("dispatch_output"),
    }
    exp = {
        "OrchestratorInput/input": oi,
        "OrchestratorOutput/output": expected_oo,
        "FrontendInput/output": fi_exp,
        "SessionStore/final_state": final,
        "SessionStore/processing_state": turn.get(("SessionStore", "processing_state")),
        "StaffConsoleInput/output": turn.get(("StaffConsoleInput", "output")),
        "DispatchInput/output": turn.get(("DispatchInput", "output")),
        "DispatchOutput/output": disp_out,
    }
    # 골든이 representation으로만 가진 것은 같은 값으로 본다
    if exp["FrontendInput/output"] is None:
        exp["FrontendInput/output"] = turn.get(("FrontendInput", "representation"))
    return {"got": got, "exp": exp}


def diff(a: Any, b: Any, path: str = "") -> list[str]:
    """두 JSON 값의 차이를 경로로. a는 구현(got), b는 골든(exp)."""
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}/{k}: 구현에 없음")
            elif k not in b:
                out.append(f"{path}/{k}: 골든에 없음")
            else:
                out += diff(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: 길이 {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}[{i}]")
    elif a != b:
        out.append(f"{path}: {json.dumps(a, ensure_ascii=False)[:80]} != {json.dumps(b, ensure_ascii=False)[:80]}")
    return out


def compare_all() -> tuple[int, dict[str, list[str]]]:
    """(비교한 쌍의 수, 다른 쌍: 키 -> 차이 목록). 키는 '케이스/tN/계약/역할'."""
    cases = load_cases()
    total = 0
    diffs: dict[str, list[str]] = {}
    for case, turns in cases.items():
        for n, turn in sorted(turns.items()):
            if ("OrchestratorInput", "input") not in turn:
                continue  # 표현·형식 예시(J6, J9, T*): 스키마 검증만 한다(test_golden.py)
            r = run_turn(case, n, turn)
            for key, exp in r["exp"].items():
                got = r["got"][key]
                if exp is None and (got is None or key == "SessionStore/processing_state"):
                    continue  # 골든에 없는 항목(처리하지 않은 턴의 processing_state 등)은 비교하지 않는다
                total += 1
                d = diff(got, exp)
                if d:
                    diffs[f"{case}/t{n}/{key}"] = d
    return total, diffs
