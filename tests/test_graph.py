"""LangGraph 그래프의 구조와 지키기로 한 성질(설계서 14절): 간선, 직렬화 가능한 상태, 체크포인터 없음, 외부 연결 없음."""
import json
import socket
import time

import pytest

from survey_service.components import turn_route as G
from survey_service.components.turn_route import COLUMNS, route
from survey_service.wiring.turn_graph import TurnCtx, initial_state

from .e2e_support import Rig, ScriptedRisk, ext, val


@pytest.fixture
def rig():
    return Rig(risk_source=ScriptedRisk())


def drawn(rig):
    return rig.orch._graph.get_graph()


def test_nodes_and_edges_match_the_design():
    rig = Rig()
    g = drawn(rig)
    nodes = {n for n in g.nodes} - {"__start__", "__end__"}
    assert nodes == {"load_session", "col_none", "col_progress", "col_paused", "col_ended", "ignore", "fail_no_session", "bind_release",
                     "extract", "validate_extraction", "plan_writes", "plan_other", "commit", "deliver_staff", "dispatch_result",
                     "respond", "finalize", "prescreen", "plan_risk"}
    # D74: 정규화·판정·기록·Plan 노드는 없다
    assert not nodes & {"parse", "normalize", "judge", "write_record", "plan", "generate"}
    edges = {(e.source, e.target) for e in g.edges}
    for e in [("__start__", "prescreen"), ("prescreen", "load_session"), ("bind_release", "plan_risk"), ("plan_risk", "commit"), ("load_session", "col_none"), ("load_session", "col_ended"), ("load_session", "fail_no_session"),
              ("col_paused", "ignore"), ("col_paused", "bind_release"), ("bind_release", "extract"), ("bind_release", "plan_other"),
              ("bind_release", "finalize"), ("extract", "validate_extraction"), ("extract", "extract"), ("extract", "finalize"),
              ("validate_extraction", "plan_writes"), ("validate_extraction", "extract"), ("validate_extraction", "finalize"),
              ("plan_writes", "commit"), ("plan_other", "commit"), ("commit", "deliver_staff"), ("commit", "finalize"),
              ("deliver_staff", "dispatch_result"), ("deliver_staff", "respond"), ("dispatch_result", "respond"),
              ("respond", "finalize"), ("finalize", "__end__"), ("ignore", "__end__"), ("fail_no_session", "__end__")]:
        assert e in edges, e
    # 저장 판단 뒤에는 앞 단계로 돌아가지 않는다(규칙 4). 되돌아가는 간선은 쓰기 확정 전의 재시도(D75)뿐이다
    order = ["prescreen", "extract", "validate_extraction", "plan_writes", "plan_other", "commit", "deliver_staff", "dispatch_result", "respond", "finalize"]
    allowed_back = {("extract", "extract"), ("validate_extraction", "extract")}
    for s_, t_ in edges:
        if s_ in order and t_ in order and (s_, t_) not in allowed_back:
            assert order.index(t_) > order.index(s_) or {s_, t_} == {"plan_writes", "plan_other"}, (s_, t_)


def test_no_checkpointer(rig):
    assert rig.orch._graph.checkpointer is None


def test_table_columns_equal_route_function():
    kinds = ["SESSION_START", "UTTERANCE", "RESUME", "END", "SESSION_END"]
    for kind in kinds:
        assert route(kind, False, None) == COLUMNS["none"].get(kind, G._DEFAULT["none"])
        for status in ("IN_PROGRESS", "PAUSED_RISK", "ENDED_COMPLETE", "ENDED_LEFT", "ENDED_STAFF", "ENDED_USER_STOP"):
            col = {"IN_PROGRESS": "progress", "PAUSED_RISK": "paused"}.get(status, "ended")
            assert route(kind, True, status) == COLUMNS[col].get(kind, G._DEFAULT[col])
    # 9-1 표의 칸 몇 개를 직접 확인
    assert route("SESSION_START", False, None).create and not route("SESSION_START", False, None).full
    assert route("RESUME", True, "PAUSED_RISK").turn_kind == "RESUME"
    assert route("UTTERANCE", True, "PAUSED_RISK").turn_kind is None and route("UTTERANCE", True, "PAUSED_RISK").keep_log
    assert route("UTTERANCE", True, "ENDED_LEFT").turn_kind is None and not route("UTTERANCE", True, "ENDED_LEFT").keep_log
    assert route("END", True, "IN_PROGRESS").turn_kind == "INTERVENTION_END"


def run_graph(rig, message=None, decision=None, turn_id="g1"):
    st = initial_state(message["session_id"] if message else decision["session_id"], turn_id, "h", message, decision)
    assert json.dumps(st)                                     # 시작 상태도 직렬화된다
    return rig.orch._graph.invoke(st, config={"configurable": {"ctx": TurnCtx()}})


def test_final_state_is_json_serializable_for_every_path(rig):
    msg = lambda **k: {"session_id": "S1", **k}
    paths = [
        dict(message=msg(kind="SESSION_START")),
        dict(message=msg(kind="UTTERANCE", utterance="안 피움")),
        dict(message=msg(kind="UTTERANCE", utterance="숨이 안 쉬어져요")),            # 위험 -> 멈춤, 직원 알림
        dict(message=msg(kind="UTTERANCE", utterance="언제 와요")),                    # 멈춘 세션: 처리 안 함
        dict(decision={"session_id": "S1", "decision": "RESUME"}),
        dict(message=msg(kind="SESSION_END")),                                         # 결과 전달
        dict(message=msg(kind="UTTERANCE", utterance="끝난 뒤")),                      # 종료 열
        dict(message={"session_id": "NEW", "kind": "SESSION_END"}),                    # 세션 없음 + 처리 안 함
    ]
    rig.model.queue[:] = [ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", False)), ext("REDIRECT", "Q1.D02", reason="OFF_TOPIC")]
    for i, p in enumerate(paths):
        final = run_graph(rig, turn_id=f"g{i}", **p)
        json.dumps(final)                                                              # 직렬화 가능
        assert final["orchestrator_output"] is not None


def test_graph_state_never_holds_resources(rig):
    final = run_graph(rig, message={"session_id": "S1", "kind": "SESSION_START"})
    def walk(x):
        if isinstance(x, dict):
            assert all(isinstance(k, str) for k in x)
            [walk(v) for v in x.values()]
        elif isinstance(x, (list, tuple)):
            [walk(v) for v in x]
        else:
            assert x is None or isinstance(x, (str, int, float, bool)), type(x)
    walk(final)


def test_turns_make_no_network_connections(rig, monkeypatch):
    """문진 발화가 밖으로 나가면 안 된다(LangChain 추적 등). 턴 처리 중 소켓 연결을 막고 그래프를 돌린다."""
    def deny(*a, **k):
        raise AssertionError("턴 처리 중 네트워크 연결 시도")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    rig.say(kind="SESSION_START")
    rig.say("안 피움", out=ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", False)))
    rig.say("숨이 안 쉬어져요", out=ext("REDIRECT", "Q1.D02", reason="OFF_TOPIC"))
    rig.say(decision="END")


def test_langchain_tracing_is_off():
    import os
    try:
        from langsmith import utils
    except ImportError:
        pytest.skip("langsmith 없음")
    for k in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        assert os.environ.get(k) in (None, "", "false", "False"), k
    assert utils.tracing_is_enabled() in (False, None)


def test_langgraph_is_pinned_in_lock_and_matches_installed():
    from pathlib import Path
    import importlib.metadata as md
    lock = (Path(__file__).parents[1] / "requirements.lock").read_text(encoding="utf-8")
    pins = [l.split("==")[1].strip() for l in lock.splitlines() if l.startswith("langgraph==")]
    assert len(pins) == 1 and md.version("langgraph") == pins[0]
    ctor = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "langgraph>=" in ctor
    assert "LANGSMITH_TRACING=false" in (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
