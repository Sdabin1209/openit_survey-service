"""Orchestrator를 실제 turn-engine과 model-runtime의 FakeModelSession으로 돌린다(문항 세트는 tests/sample_bank.py)."""
import copy
import json
import threading

import jsonschema
import pytest

import survey_schema
import turn_engine

from survey_service.errors import ReleaseUnavailable, TurnIdConflict
from survey_service.orchestrator import Orchestrator, route
from survey_service.ports import Engine, LoadedRelease
from survey_service.release.registry import ReleaseRegistry
from survey_service.store.memory import MemorySessionStore
from survey_service.store.sqlite import SqliteSessionStore

from .e2e_support import Rig, ScriptedModel, item, make_session, other
from .fakes import FaultyStore, RecordingConsole, RecordingDispatcher, RecordingSink
from .sample_bank import sample_bank

RISK = other(0, "RISK_SIGNAL", "숨이 안 쉬어져요")


def le(o):
    return o.orchestrator_output["log_entry"]


def rec_of(doc, qid):
    return [r for r in doc["answer_records"] if r["slot"]["question_id"] == qid]


# ---------------------------------------------------------------- 처리 표 9-1
def prepare(rig, state):
    if state == "none":
        return
    rig.say(kind="SESSION_START")
    if state == "paused":
        rig.say("숨이 안 쉬어져요", items=[RISK])
        assert rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"
    elif state == "ended":
        rig.say(kind="SESSION_END")
        assert rig.doc()["progress_record"]["session_status"].startswith("ENDED")


# (입력) -> {세션 상태: (turn_kind, 로그를 남기는가)}
TABLE = {
    "START": {"none": ("SESSION_START", True), "progress": ("SESSION_START", True), "paused": (None, True), "ended": (None, False)},
    "UTTER": {"none": ("UTTERANCE", True), "progress": ("UTTERANCE", True), "paused": (None, True), "ended": (None, False)},
    "RESUME": {"none": (None, False), "progress": (None, True), "paused": ("RESUME", True), "ended": (None, False)},
    "END": {"none": (None, False), "progress": ("INTERVENTION_END", True), "paused": ("INTERVENTION_END", True), "ended": (None, False)},
    "LEFT": {"none": (None, False), "progress": ("END_LEFT", True), "paused": ("END_LEFT", True), "ended": (None, False)},
}


@pytest.mark.parametrize("inp", list(TABLE))
@pytest.mark.parametrize("state", ["none", "progress", "paused", "ended"])
def test_processing_table(inp, state):
    rig = Rig()
    prepare(rig, "progress" if state == "progress" else state)
    before = len(rig.doc()["dialogue_log"]) if state != "none" else 0
    kind, logged = TABLE[inp][state]
    if inp == "START":
        o = rig.say(kind="SESSION_START")
    elif inp == "UTTER":
        o = rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")])
    elif inp == "RESUME":
        o = rig.say(decision="RESUME")
    elif inp == "END":
        o = rig.say(decision="END")
    else:
        o = rig.say(kind="SESSION_END")
    oo = o.orchestrator_output
    assert oo["turn_kind"] == kind
    after = rig.store.load("S1")
    n_after = len(after["state"]["dialogue_log"]) if after["status"] == "OK" else 0
    assert (n_after - before) == (1 if logged else 0)
    assert oo["turn_failure_signal"] is None
    if inp in ("RESUME", "END"):
        assert oo["intervention_result"]["result"] == ("APPLIED" if kind else "IGNORED")
        assert o.frontend_input is None or kind
    else:
        assert oo["intervention_result"] is None
    if kind is None:
        # 처리 안 함: 어떤 컴포넌트도 부르지 않고 응답이 없다
        assert o.frontend_input in (None, {"response_text": None, "turn_failure_signal": None})
        assert o.trace.get("parser_input") is None and o.trace.get("planner_input") is None
        if state == "none":
            assert after["status"] == "NOT_FOUND"          # 없는 세션은 만들지 않는다
    if inp == "START" and state == "none":
        assert oo["session_info_created"] == {"session_id": "S1", "question_bank_version": 1}
    else:
        assert oo["session_info_created"] is None or (inp == "UTTER" and state == "none")


def test_input_must_be_exactly_one():
    rig = Rig()
    with pytest.raises(ValueError):
        rig.orch.handle_turn(turn_id="t")
    with pytest.raises(ValueError):
        rig.orch.handle_turn(turn_id="t", frontend_message={"session_id": "S", "kind": "SESSION_START"},
                             intervention_decision={"session_id": "S", "decision": "END"})
    with pytest.raises(ValueError):
        rig.orch.handle_turn(turn_id="t", frontend_message={"session_id": "S", "kind": "UTTERANCE"})
    with pytest.raises(ValueError):
        rig.orch.handle_turn(turn_id="t", frontend_message={"session_id": "S", "kind": "NOPE"})
    with pytest.raises(ValueError):
        rig.orch.handle_turn(turn_id="", frontend_message={"session_id": "S", "kind": "SESSION_START"})


def test_route_function_matches_table_cells():
    assert route("RESUME", True, "IN_PROGRESS").turn_kind is None
    assert route("UTTERANCE", False, None).create and route("UTTERANCE", False, None).full
    assert route("SESSION_START", False, None).create and not route("SESSION_START", False, None).full
    assert not route("UTTERANCE", True, "ENDED_LEFT").keep_log and route("UTTERANCE", True, "PAUSED_RISK").keep_log


# ---------------------------------------------------------------- 정상 흐름
def test_full_conversation_with_real_engine():
    rig = Rig()
    o = rig.say(kind="SESSION_START")
    assert le(o)["directive"]["action"] == "ASK_NEXT" and o.frontend_input["response_text"]
    assert rig.store.release_id_of("S1") == "rel-e2e-1"
    o = rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")])
    assert rec_of(rig.doc(), "Q1")[0]["current_value"] == {"answer": "안 피움"}
    o = rig.say("일곱 시간", items=[item(0, "Q5", "시간", "일곱 시간", "일곱 시간")])
    # 충돌: 새 값은 확인 대기가 되고 현재값은 지켜진다
    o = rig.say("8시간", items=[item(0, "Q5", "시간", "8시간", "8시간")])
    q5 = rec_of(rig.doc(), "Q5")[0]
    assert le(o)["directive"]["action"] == "ASK_CONFIRM"
    assert q5["current_value"] == {"answer": {"amount": 7, "unit": "시간"}} and q5["pending"]["value"] == {"amount": 8, "unit": "시간"}
    assert le(o)["directive"]["target_pending_id"] == q5["pending"]["pending_id"]
    # 확인 응답: 대기 값이 현재값이 되고 이전 답이 이력으로 간다
    o = rig.say("네", items=[other(0, "CONFIRM_YES", "네")])
    q5 = rec_of(rig.doc(), "Q5")[0]
    assert q5["current_value"] == {"answer": {"amount": 8, "unit": "시간"}} and q5["pending"] is None
    assert q5["history"] == [{"amount": 7, "unit": "시간"}]
    doc = rig.doc()
    assert len(doc["dialogue_log"]) == 5


def test_risk_pauses_alerts_and_resume_and_end_dispatch():
    rig = Rig()
    rig.say(kind="SESSION_START")
    o = rig.say("숨이 안 쉬어져요", items=[RISK])
    assert le(o)["staff_alert"]["kind"] == "RISK" and le(o)["staff_alert_delivery"] == "SUCCESS"
    assert rig.console.inputs[0]["staff_alert"]["staff_alert"]["kind"] == "RISK"
    assert rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"
    assert rig.doc()["progress_record"]["pause_history"][0]["kind"] == "PAUSE"
    o = rig.say("언제 와요", items=[])
    assert le(o)["handling"] == "IGNORED" and le(o)["turn_result"] == "NOT_APPLICABLE" and o.trace["parser_input"] is None
    o = rig.say(decision="RESUME")
    assert o.orchestrator_output["intervention_result"]["result"] == "APPLIED"
    assert rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS"
    assert rig.console.inputs[-1] == {"staff_alert": None, "turn_failure_signal": None,
                                      "intervention_result": {"session_id": "S1", "result": "APPLIED"}}
    o = rig.say(decision="END")
    assert o.orchestrator_output["dispatch_called"] is True and len(rig.dispatcher.inputs) == 1
    assert rig.dispatcher.inputs[0]["session_status"] == "ENDED_STAFF"
    assert rig.doc()["dispatch_log"] == [{"succeeded": True}]
    assert o.trace["dispatch_output"] == {"dispatch_record": {"succeeded": True}}


def test_staff_alert_is_delivered_before_response_generation():
    rig = Rig()
    rig.say(kind="SESSION_START")
    seen = []
    real = rig.release.session.generate
    rig.release.session.generate = lambda i: (seen.append(len(rig.console.inputs)), real(i))[1]
    rig.say("숨이 안 쉬어져요", items=[RISK])
    assert seen == [1]


# ---------------------------------------------------------------- 실패
def engine_with(**over):
    base = dict(normalize=turn_engine.normalize, judge=turn_engine.judge, write_record=turn_engine.write_record, plan=turn_engine.plan)
    base.update(over)
    return Engine(**base)


def boom(_):
    raise RuntimeError("시험용 실패")


@pytest.mark.parametrize("step,over", [("NORMALIZE", {"normalize": boom}), ("JUDGE", {"judge": boom}),
                                       ("RECORD_WRITE", {"write_record": boom}), ("PLAN", {"plan": boom})])
def test_failure_before_commit_changes_nothing(step, over):
    rig = Rig(engine=engine_with(**over))
    rig.say(kind="SESSION_START")
    snap = rig.doc()
    o = rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")])
    sig = {"session_id": "S1", "write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}
    assert o.orchestrator_output["turn_failure_signal"] == sig and o.frontend_input == {"response_text": None, "turn_failure_signal": sig}
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "BEFORE_COMMIT", "step": step}]}
    doc = rig.doc()
    assert doc["answer_records"] == snap["answer_records"] and doc["progress_record"] == snap["progress_record"]
    assert doc["planner_record"] == snap["planner_record"]
    assert len(doc["dialogue_log"]) == len(snap["dialogue_log"]) + 1     # 실패한 턴도 로그에 남긴다
    assert not rig.dispatcher.inputs


def test_parser_failure():
    rig = Rig()
    rig.say(kind="SESSION_START")
    rig.release.session.parse = boom
    o = rig.say("안 피움", items=[])
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]}
    assert o.frontend_input["response_text"] is None


def test_commit_failure_skips_everything_after_commit():
    rig = Rig()
    rig.say(kind="SESSION_START")
    n_console = len(rig.console.inputs)
    rig.store.fail_commit = None
    faulty = FaultyStore(rig.store, fail_commit="X")
    rig.orch.store = faulty
    o = rig.say("숨이 안 쉬어져요", items=[RISK])                       # 위험 알림이 있었어야 하는 턴
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "BEFORE_COMMIT", "step": "COMMIT"}]}
    assert o.orchestrator_output["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    assert o.frontend_input["response_text"] is None
    assert le(o)["staff_alert"] is None and le(o)["staff_alert_delivery"] == "NOT_APPLICABLE"
    assert [i for i in rig.console.inputs[n_console:] if i["staff_alert"]] == []
    assert rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS"


def test_commit_unknown_outcome_is_not_cached_so_retry_reruns():
    rig = Rig()
    rig.say(kind="SESSION_START")
    faulty = FaultyStore(rig.store, fail_commit="UNKNOWN")
    rig.orch.store = faulty
    o = rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")], turn_id="T-U")
    assert not o.cacheable and o.orchestrator_output["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    faulty.fail_commit = None
    o2 = rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")], turn_id="T-U")
    assert not o2.replayed and rec_of(rig.doc(), "Q1")[0]["current_value"] == {"answer": "안 피움"}


def test_staff_delivery_failure_still_responds_and_signals():
    rig = Rig()
    rig.say(kind="SESSION_START")
    rig.console.fail = True
    o = rig.say("숨이 안 쉬어져요", items=[RISK])
    assert o.orchestrator_output["turn_failure_signal"] == {"session_id": "S1", "write_committed": "COMMITTED", "staff_alert": "DELIVERY_FAILED"}
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "AFTER_COMMIT", "step": "DELIVER_STAFF"}]}
    assert le(o)["staff_alert_delivery"] == "FAILURE" and o.frontend_input["response_text"]
    assert rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"        # 쓰기는 반영됐다
    assert o.staff_console_input["staff_alert"] is None and o.staff_console_input["turn_failure_signal"]


def test_generator_failure_after_commit():
    rig = Rig()
    rig.say(kind="SESSION_START")
    rig.release.session.generate = boom
    o = rig.say("숨이 안 쉬어져요", items=[RISK])
    assert o.orchestrator_output["turn_failure_signal"] == {"session_id": "S1", "write_committed": "COMMITTED", "staff_alert": "DELIVERED"}
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "AFTER_COMMIT", "step": "LoRA-B"}]}
    assert o.frontend_input["response_text"] is None and len(rig.console.inputs) >= 1


def test_frontend_sink_failure():
    sink = RecordingSink(fail=True)
    rig = Rig(frontend_sink=sink)
    o = rig.say(kind="SESSION_START")
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "AFTER_COMMIT", "step": "DELIVER_FRONT"}]}
    assert o.frontend_input["response_text"] is None and le(o)["response_text"]
    ok = RecordingSink()
    rig2 = Rig(frontend_sink=ok)
    o2 = rig2.say(kind="SESSION_START")
    assert ok.inputs == [o2.frontend_input] and o2.orchestrator_output["turn_failure_signal"] is None


def test_dispatch_failure_is_recorded_not_a_turn_failure():
    rig = Rig()
    rig.dispatcher.fail = True
    rig.say(kind="SESSION_START")
    o = rig.say(kind="SESSION_END")
    assert o.orchestrator_output["dispatch_called"] is True and o.orchestrator_output["turn_failure_signal"] is None
    assert rig.doc()["dispatch_log"] == [{"succeeded": False}]


def test_unconfigured_staff_console_fails_honestly():
    rig = Rig()
    rig.orch.staff_console = Orchestrator(MemorySessionStore(), rig.registry).staff_console   # 기본값
    rig.say(kind="SESSION_START")
    o = rig.say("숨이 안 쉬어져요", items=[RISK])
    assert o.orchestrator_output["turn_failure_signal"]["staff_alert"] == "DELIVERY_FAILED"


def test_session_creation_failure_leaves_no_log():
    rig = Rig()
    rig.orch.store = FaultyStore(rig.store, fail_create=True)
    o = rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")])
    assert o.orchestrator_output["log_entry"] is None and o.orchestrator_output["turn_kind"] == "UTTERANCE"
    assert o.orchestrator_output["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    assert rig.store.load("S1")["status"] == "NOT_FOUND"


def test_session_bound_release_unavailable_fails_first_step():
    rig = Rig()
    rig.say(kind="SESSION_START")
    rig.registry._loaded.clear()
    o = rig.say("안 피움", items=[])
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]}


def test_writer_decision_that_cannot_be_applied_changes_nothing():
    def bad_writer(inp):
        res = turn_engine.write_record(inp)
        res["store_decisions"].append({"slot": res["store_decisions"][0]["slot"], "action": "ACCEPT_PENDING", "value": "x",
                                       "pending_id": "none", "risk_mark": None})
        return res
    rig = Rig(engine=engine_with(write_record=bad_writer))
    rig.say(kind="SESSION_START")
    snap = rig.doc()["answer_records"]
    o = rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")])
    assert le(o)["turn_result"]["failed_steps"][0]["step"] == "RECORD_WRITE"
    assert rig.doc()["answer_records"] == snap


# ---------------------------------------------------------------- 멱등과 직렬화
def test_same_turn_id_returns_same_result_without_second_write():
    rig = Rig()
    rig.say(kind="SESSION_START")
    items = [item(0, "Q1", "상태", "안 피움", "안 피움")]
    a = rig.say("안 피움", items=items, turn_id="T-1")
    doc1 = rig.doc()
    consoles = len(rig.console.inputs)
    b = rig.say("안 피움", items=items, turn_id="T-1")          # 파서 큐에 한 번 더 넣었지만 소비되면 안 된다
    assert b.replayed and b.orchestrator_output == a.orchestrator_output and b.frontend_input == a.frontend_input
    assert rig.doc() == doc1 and len(rig.console.inputs) == consoles
    assert len(rig.model.parse_queue) == 1                           # 모델을 다시 부르지 않았다


def test_same_turn_id_different_input_conflicts():
    rig = Rig()
    rig.say(kind="SESSION_START", turn_id="T-1")
    with pytest.raises(TurnIdConflict):
        rig.say("안녕", items=[], turn_id="T-1")


def test_replay_of_ignored_turn_does_not_log_twice():
    rig = Rig()
    prepare(rig, "paused")
    n = len(rig.doc()["dialogue_log"])
    a = rig.say("언제 와요", items=[], turn_id="T-I")
    b = rig.say("언제 와요", items=[], turn_id="T-I")
    assert b.replayed and b.orchestrator_output == a.orchestrator_output
    assert len(rig.doc()["dialogue_log"]) == n + 1


def test_committed_but_no_result_recovers_without_rewriting():
    rig = Rig()
    rig.say(kind="SESSION_START")
    msg = {"session_id": "S1", "kind": "UTTERANCE", "utterance": "안 피움"}
    from boundary_contracts import canonical
    h = canonical.sha256_hash({"m": msg, "d": None})
    st = rig.doc()
    rig.store._turns[("S1", "T-R")] = __import__("survey_service.store.base", fromlist=["TurnRecord"]).TurnRecord(h, True, None)
    o = rig.orch.handle_turn(turn_id="T-R", frontend_message=msg)
    assert o.replayed and o.orchestrator_output["turn_failure_signal"]["write_committed"] == "COMMITTED"
    assert rig.doc() == st


def test_same_session_turns_are_serialized():
    rig = Rig()
    rig.say(kind="SESSION_START")
    active, peak = [0], [0]
    lock = threading.Lock()
    real = rig.release.session.parse

    def slow_parse(inp):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        import time
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return {"items": []}
    rig.release.session.parse = slow_parse
    ts = [threading.Thread(target=lambda i=i: rig.orch.handle_turn(turn_id=f"c{i}", frontend_message={"session_id": "S1", "kind": "UTTERANCE", "utterance": f"x{i}"})) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1 and len(rig.doc()["dialogue_log"]) == 7


# ---------------------------------------------------------------- 릴리스 전환
def test_new_release_applies_to_new_sessions_only():
    rig = Rig()
    rig.say(kind="SESSION_START", sid="OLD")
    new_model = ScriptedModel()
    rel2 = LoadedRelease("rel-e2e-2", {}, sample_bank(version=2), make_session(new_model))
    rig.registry.register(rel2, activate=True)
    o_old = rig.say(kind="SESSION_START", sid="OLD")                 # 진행 중인 세션은 자기 릴리스로 계속
    assert le(o_old)["question_bank_version"] == 1 and rig.store.release_id_of("OLD") == "rel-e2e-1"
    assert o_old.trace["orchestrator_input"]["latest_question_bank_version"] == 2
    assert o_old.release_id == "rel-e2e-1"
    o_new = rig.say(kind="SESSION_START", sid="NEW")
    assert le(o_new)["question_bank_version"] == 2 and rig.store.release_id_of("NEW") == "rel-e2e-2" and o_new.release_id == "rel-e2e-2"
    assert rig.doc("OLD")["session_info"]["question_bank_version"] == 1


# ---------------------------------------------------------------- Trace
def test_trace_is_schema_valid_and_complete():
    got = []

    class Sink:
        def submit(self, release_id, trace):
            got.append((release_id, copy.deepcopy(trace)))
    rig = Rig(trace_sink=Sink())
    v = jsonschema.Draft202012Validator(survey_schema.load_schema("TurnTrace"))
    rig.say(kind="SESSION_START")
    rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")])
    rig.say("숨이 안 쉬어져요", items=[RISK])
    rig.say("언제 와요", items=[])
    rig.say(decision="END")
    rig.say(decision="END", turn_id="ign")                    # 끝난 세션: 처리 안 함
    assert len(got) == 6
    for rid, tr in got:
        assert rid == "rel-e2e-1"
        errs = list(v.iter_errors(tr))
        assert not errs, errs[0].message[:200]
        assert tr["contract_version"] == 1 and tr["orchestrator_output"] and tr["orchestrator_input"]
    full = got[1][1]
    for k in ["parser_input", "parser_output", "normalizer_input", "normalizer_output", "judge_input", "judge_output",
              "record_writer_input", "record_writer_output", "planner_input", "planner_output", "generator_input",
              "generator_output", "frontend_input", "session_before", "session_after", "log_entry",
              "persistence_load_request", "persistence_load_result", "persistence_commit_request", "persistence_commit_result"]:
        assert full[k] is not None, k
    assert full["dispatch_input"] is None and got[4][1]["dispatch_input"] is not None
    assert got[5][1]["parser_input"] is None and got[5][1]["log_entry"] is None
    assert got[0][1]["session_before"]["dialogue_log"] == [] and got[1][1]["session_after"]["dialogue_log"][-1] == got[1][1]["log_entry"]


def test_trace_sink_failure_does_not_fail_the_turn():
    class Bad:
        def submit(self, *a):
            raise RuntimeError("outbox 사용 불가")
    rig = Rig(trace_sink=Bad())
    o = rig.say(kind="SESSION_START")
    assert o.frontend_input["response_text"] and rig.orch.counters["trace_enqueue_failed"] == 1


def test_replayed_turn_is_not_traced_twice():
    got = []

    class Sink:
        def submit(self, release_id, trace):
            got.append(1)
    rig = Rig(trace_sink=Sink())
    rig.say(kind="SESSION_START", turn_id="x")
    rig.say(kind="SESSION_START", turn_id="x")
    assert got == [1]


# ---------------------------------------------------------------- SQLite 저장소로 같은 흐름
def test_conversation_on_sqlite_survives_restart(tmp_path):
    db = str(tmp_path / "s.db")
    rig = Rig(store=SqliteSessionStore(db))
    rig.say(kind="SESSION_START")
    rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")], turn_id="T-A")
    first = rig.doc()
    rig.store.close()
    rig2 = Rig(store=SqliteSessionStore(db))
    assert rig2.doc() == first
    again = rig2.orch.handle_turn(turn_id="T-A", frontend_message={"session_id": "S1", "kind": "UTTERANCE", "utterance": "안 피움"})
    assert again.replayed and rig2.doc() == first
    o = rig2.say("일곱 시간", items=[item(0, "Q5", "시간", "일곱 시간", "일곱 시간")], turn_id="T-B")
    assert rec_of(rig2.doc(), "Q5")[0]["current_value"] == {"answer": {"amount": 7, "unit": "시간"}}
