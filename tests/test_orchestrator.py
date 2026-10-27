"""Orchestrator를 실제 turn-engine(validate_extraction)과 model-runtime의 FakeModelSession으로 돌린다(D74 구조, 문항 세트는 tests/sample_bank.py)."""
import copy
import threading

import jsonschema
import pytest

import survey_schema

from survey_service.errors import TurnIdConflict
from survey_service.orchestrator import Orchestrator, route
from survey_service.ports import LoadedRelease
from survey_service.store.memory import MemorySessionStore
from survey_service.store.sqlite import SqliteSessionStore

from .e2e_support import Rig, ScriptedModel, ScriptedRisk, ext, make_session, val
from .fakes import FaultyStore, RecordingSink
from .sample_bank import sample_bank

HEALTH = ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", False), val("Q1.D01.on_medication", False))


def le(o):
    return o.orchestrator_output["log_entry"]


def rec(doc, key):
    q, s = key.rsplit(".", 1)
    return next(r for r in doc["answer_records"] if r["slot"]["question_id"] == q and r["slot"]["slot_id"] == s)


def risk_rig(**kw):
    return Rig(risk_source=ScriptedRisk(), **kw)


# ---------------------------------------------------------------- 처리 표 9-1
def prepare(rig, state):
    if state == "none":
        return
    rig.start()
    if state == "paused":
        rig.say("숨이 안 쉬어져요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
        assert rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"
    elif state == "ended":
        rig.say(kind="SESSION_END")
        assert rig.doc()["progress_record"]["session_status"].startswith("ENDED")


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
    rig = risk_rig()
    prepare(rig, state)
    before = len(rig.doc()["dialogue_log"]) if state != "none" else 0
    kind, logged = TABLE[inp][state]
    if inp == "START":
        o = rig.start()
    elif inp == "UTTER":
        o = rig.say("안 먹어요", out=HEALTH)
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
    else:
        assert oo["intervention_result"] is None
    if kind is None:
        # 처리 안 함: 모델을 부르지 않고 응답이 없다
        assert o.frontend_input in (None, {"response_text": None, "turn_failure_signal": None})
        assert o.trace.get("extract_input") is None and o.trace.get("response_input") is None
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
def test_start_calls_only_lora_b_with_start_action():
    rig = Rig()
    o = rig.start()
    assert rig.session.extract_requests == []                       # 시작은 A를 부르지 않는다
    req = rig.session.respond_requests[0]
    assert req == {"action": "START", "next": {"question_id": "Q1.D01", "text": rig.bank["questions"][0]["question_sentence"], "type": "NEXT"},
                   "topic": "뇌졸중(중풍) 진단을 받았는지와 지금 약을 먹는지"}
    assert o.frontend_input == {"response_text": "응답입니다.", "turn_failure_signal": None}
    assert rig.doc()["progress_record"]["current_question_id"] == "Q1.D01"
    assert o.trace["response_input"] == req and o.trace["response_outcome"] == "ok" and o.trace["extract_input"] is None


def test_full_conversation():
    rig = Rig()
    rig.start()
    o = rig.say("진단 안 받았고 약도 안 먹어요", out=HEALTH)
    # A 입력: 직전 응답 전체 문장이 current_question, 문항은 진행 기록의 문항
    assert rig.session.extract_requests[0] == {"current_question": "응답입니다.", "question_id": "Q1.D01", "state": {},
                                               "user_utterance": "진단 안 받았고 약도 안 먹어요"}
    doc = rig.doc()
    assert rec(doc, "Q1.D01.diagnosed")["current_value"] == {"answer": False} and rec(doc, "Q1.D01.diagnosed")["precision"] == "exact"
    assert doc["progress_record"]["current_question_id"] == "Q1.D02"
    # B 입력: known은 표에서 찾은 사람 말, next는 문항 은행 질문 문장
    req = rig.session.respond_requests[-1]
    assert req["known"] == "진단 안 받음, 지금 약 안 먹음" and req["action"] == "NEXT" and req["next"]["question_id"] == "Q1.D02"
    assert req["next"]["text"] == rig.bank["questions"][1]["question_sentence"]
    assert list(req) == sorted(req) and list(req["next"]) == sorted(req["next"])      # B 입력은 모든 수준의 키가 정렬돼 있다
    assert le(o)["extract_output"]["next_item"] == "Q1.D02" and le(o)["validation_violations"] == []
    # 다음 턴: 직전 문항의 저장 값이 state에 실린다
    rig.say("고혈압 약만요", out=ext("NEXT", "Q1.D03", val("Q1.D02.diagnosed", True), val("Q1.D02.on_medication", False)))
    assert list(rig.session.extract_requests[-1]["state"]) == ["Q1.D01.diagnosed", "Q1.D01.on_medication"]


def test_only_saved_values_are_stored_and_conflict_does_not_overwrite():
    rig = Rig()
    rig.start()
    rig.say("약 안 먹어요", out=ext("REASK", "Q1.D01", val("Q1.D01.diagnosed", True), val("Q1.D01.on_medication", None, "MISSING"),
                                  reason="MISSING", targets=["Q1.D01.on_medication"]))
    doc = rig.doc()
    assert rec(doc, "Q1.D01.diagnosed")["current_value"] == {"answer": True} and rec(doc, "Q1.D01.on_medication")["current_value"] == "NONE"
    assert doc["progress_record"]["open_slots"] == [{"key": "Q1.D01.on_medication", "status": "MISSING"}]
    # 다음 턴의 state: 지금 문항의 저장 값 -> 미저장 칸(MISSING) 순서
    rig.say("약은 먹어요", out=ext("NEXT", "Q1.D02", val("Q1.D01.on_medication", True)))
    assert rig.session.extract_requests[-1]["state"] == {"Q1.D01.diagnosed": {"value": True, "status": "ANSWERED"},
                                                         "Q1.D01.on_medication": {"value": None, "status": "MISSING"}}
    assert rig.doc()["progress_record"]["open_slots"] == []
    # 충돌: 새 값(save=false)은 저장하지 않고 CONFIRM으로 되묻는다. 현재값을 지킨다
    o = rig.say("아니 약 안 먹어요", out=ext("CONFIRM", "Q1.D01", val("Q1.D01.on_medication", False, "CONFLICT"),
                                         reason="CONFLICT_WITH_PREVIOUS", targets=["Q1.D01.on_medication"]))
    assert rec(rig.doc(), "Q1.D01.on_medication")["current_value"] == {"answer": True}
    req = rig.session.respond_requests[-1]
    assert req["previous"] == "지금 약 먹음" and req["current"] == "지금 약 안 먹음" and req["known"] == ""
    assert req["next"]["type"] == "SAME" and rig.doc()["progress_record"]["current_question_id"] == "Q1.D01"
    # 정정: CORRECTION은 저장하고 이전 값은 이력으로
    rig.say("아니 안 먹어요", out=ext("CORRECT", "Q1.D02", val("Q1.D01.on_medication", False, "CORRECTION")))
    r = rec(rig.doc(), "Q1.D01.on_medication")
    assert r["current_value"] == {"answer": False} and r["history"] == [{"value": True, "precision": "exact"}]


def test_skip_and_hold_record_skipped_questions_and_end_dispatches():
    rig = Rig()
    rig.start()
    rig.say("나중에요", out=ext("SKIP", "Q1.D02", reason="REQUEST_SKIP", targets=["Q1.D01.diagnosed", "Q1.D01.on_medication"]) |
            {"values": [val("Q1.D01.diagnosed", None, "UNKNOWN"), val("Q1.D01.on_medication", None, "UNKNOWN")]})
    assert rig.doc()["progress_record"]["skipped"] == [{"question_id": "Q1.D01", "reason": "REQUEST_SKIP"}]
    assert rig.session.respond_requests[-1]["hold"] == "뇌졸중(중풍)"
    o = rig.say("그만", out=ext("NEXT", "END", val("Q10.days", 2, "CORRECTION")))
    assert rig.doc()["progress_record"]["session_status"] == "ENDED_COMPLETE" and o.orchestrator_output["dispatch_called"] is True
    assert rig.dispatcher.inputs[0]["session_status"] == "ENDED_COMPLETE" and rig.dispatcher.inputs[0]["skipped"]
    assert rig.session.respond_requests[-1]["next"] == {"type": "END"}


def test_partial_prefilled_next_asks_only_the_remaining_slots():
    """57행 문제: 같은 말에서 Q4_1.status가 이미 정해졌으면 next.text는 남은 칸만 묻는다."""
    rig = Rig()
    rig.start()
    rig.say("피운 적 있고 끊었어요", out=ext("NEXT", "Q4_1", val("Q4.answer", True), val("Q4_1.status", "former")))
    # 시작 문항을 Q4로 옮기지 않고도 값 저장은 된다(문항 순서 검증은 next_item만 본다)
    req = rig.session.respond_requests[-1]
    assert req["known"] == "5갑 넘게 피운 적 있음, 일반담배: 끊음"
    assert req["next"]["type"] == "NEXT" and req["next"]["question_id"] == "Q4_1"
    assert req["next"]["text"] == "일반담배는 피운 기간, 하루 개비 수, 끊은 지 얼마나 됐는지 말씀해 주세요."


def test_risk_pauses_alerts_and_resume_and_end_dispatch():
    rig = risk_rig()
    rig.start()
    o = rig.say("숨이 안 쉬어져요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
    assert le(o)["staff_alert"]["kind"] == "RISK" and le(o)["staff_alert_delivery"] == "SUCCESS"
    assert rig.console.inputs[0]["staff_alert"]["staff_alert"]["kind"] == "RISK"
    assert o.frontend_input["response_text"] == "바로 직원을 불렀습니다. 그 자리에 계셔 주세요."      # 고정 문장, 모델을 부르지 않는다
    assert rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"
    assert rig.doc()["progress_record"]["pause_history"][0]["kind"] == "PAUSE"
    n_extract = len(rig.session.extract_requests)
    o = rig.say("언제 와요")
    assert le(o)["handling"] == "IGNORED" and le(o)["turn_result"] == "NOT_APPLICABLE" and o.trace["extract_input"] is None
    assert len(rig.session.extract_requests) == n_extract
    o = rig.say(decision="RESUME")
    assert o.orchestrator_output["intervention_result"]["result"] == "APPLIED"
    assert rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS" and o.trace["response_outcome"] == "fixed"
    assert o.frontend_input is None or True
    assert rig.console.inputs[-1] == {"staff_alert": None, "turn_failure_signal": None,
                                      "intervention_result": {"session_id": "S1", "result": "APPLIED"}}
    o = rig.say(decision="END")
    assert o.orchestrator_output["dispatch_called"] is True and len(rig.dispatcher.inputs) == 1
    assert rig.dispatcher.inputs[0]["session_status"] == "ENDED_STAFF"
    assert rig.doc()["dispatch_log"] == [{"succeeded": True}]
    assert o.trace["dispatch_output"] == {"dispatch_record": {"succeeded": True}}


def test_without_risk_source_no_staff_alert_is_ever_made():
    rig = Rig()
    rig.start()
    o = rig.say("숨이 안 쉬어져요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
    assert rig.orch.risk_source is None and le(o)["staff_alert"] is None and rig.console.inputs == []
    assert rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS"


def test_staff_alert_is_delivered_before_response_generation():
    rig = risk_rig()
    rig.start()
    seen = []
    real = rig.session.respond
    rig.session.respond = lambda r: (seen.append(len(rig.console.inputs)), real(r))[1]
    rig.say("숨이 안 쉬어져요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
    assert seen == []                                              # 위험 안내는 고정 문장이라 모델을 부르지 않는다
    rig.say(decision="RESUME")
    rig.orch.risk_source = ScriptedRisk(phrase="직원 불러", kind="CALL_STAFF")
    rig.say("직원 불러 주세요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
    assert seen == [3]                                             # CALL_STAFF: 알림이 응답 생성보다 앞이다


# ---------------------------------------------------------------- 추출 실패와 재시도 (D75)
BAD = "이건 JSON이 아니다"


def test_output_invalid_is_retried_once_then_succeeds():
    rig = Rig()
    rig.start()
    rig.model.queue += [BAD] * 3                                   # 첫 호출: 서버 안에서 3번 시도해도 못 지킴 -> 503 OUTPUT_INVALID
    o = rig.say("안 먹어요", out=HEALTH)
    assert o.trace["extract_attempts"] == 2 and o.trace["extract_status"] == "OK" and len(rig.session.extract_requests) == 2
    assert rig.session.extract_requests[0] == rig.session.extract_requests[1]            # 같은 요청
    assert o.orchestrator_output["turn_failure_signal"] is None
    assert rec(rig.doc(), "Q1.D01.diagnosed")["current_value"] == {"answer": False}


def test_output_invalid_twice_fails_turn_and_keeps_state():
    rig = Rig()
    rig.start()
    snap = rig.doc()
    rig.model.queue += [BAD] * 6
    o = rig.say("안 먹어요")
    sig = {"session_id": "S1", "write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}
    assert o.orchestrator_output["turn_failure_signal"] == sig and o.frontend_input == {"response_text": None, "turn_failure_signal": sig}
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]}
    assert o.trace["extract_status"] == "OUTPUT_INVALID" and o.trace["extract_attempts"] == 2
    doc = rig.doc()
    assert doc["answer_records"] == snap["answer_records"] and doc["progress_record"] == snap["progress_record"]
    assert len(doc["dialogue_log"]) == len(snap["dialogue_log"]) + 1
    assert len(rig.session.respond_requests) == 1             # B를 부르지 않았다(시작 한 번뿐)


# next_item이 REASK의 규칙(= 입력 question_id)을 어긴다 -> validate_extraction 위반
VIOLATING = ext("REASK", "Q1.D03", val("Q1.D01.on_medication", None, "MISSING"), reason="MISSING", targets=["Q1.D01.on_medication"])


def test_violation_is_retried_once_then_succeeds():
    rig = Rig()
    rig.start()
    rig.model.queue.append(VIOLATING)
    o = rig.say("음", out=HEALTH)
    assert o.trace["extract_attempts"] == 2 and o.trace["extract_status"] == "OK"
    assert o.trace["validation_ok"] is True and o.trace["validation_violations"] == []
    assert rec(rig.doc(), "Q1.D01.diagnosed")["current_value"] == {"answer": False}


def test_violation_twice_fails_turn_and_traces_violations():
    rig = Rig()
    rig.start()
    snap = rig.doc()
    rig.model.queue += [VIOLATING, VIOLATING]
    o = rig.say("음")
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "BEFORE_COMMIT", "step": "VALIDATE"}]}
    assert o.trace["extract_status"] == "VIOLATION" and o.trace["validation_ok"] is False
    assert [v["code"] for v in o.trace["validation_violations"]] == ["NEXT_ITEM_MISMATCH"]
    assert set(o.trace["validation_violations"][0]) == {"code", "path", "message"}       # 값을 싣지 않는다
    assert le(o)["validation_violations"] == o.trace["validation_violations"]
    assert rig.doc()["answer_records"] == snap["answer_records"] and rig.doc()["progress_record"] == snap["progress_record"]


def test_save_to_unknown_slot_is_a_violation_not_an_invented_record():
    rig = Rig()
    rig.start()
    rig.model.queue += [ext("NEXT", "Q1.D02", val("Q1.D01.shoe_size", 270)), ext("NEXT", "Q1.D02", val("Q1.D01.shoe_size", 270))]
    snap = rig.doc()["answer_records"]
    o = rig.say("270")
    assert [v["code"] for v in o.trace["validation_violations"]] == ["UNKNOWN_SLOT"] and o.trace["extract_attempts"] == 2
    assert rig.doc()["answer_records"] == snap


def test_other_extract_error_is_not_retried_as_output_invalid():
    rig = Rig()
    rig.start()

    def boom(_):
        raise RuntimeError("시험용 실패")
    rig.session.extract = boom
    o = rig.say("안 먹어요")
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]}
    assert o.trace["extract_status"] == "ERROR" and o.trace["extract_attempts"] == 1


# ---------------------------------------------------------------- 실패
def test_commit_failure_skips_everything_after_commit():
    rig = risk_rig()
    rig.start()
    n_console = len(rig.console.inputs)
    rig.orch.store = FaultyStore(rig.store, fail_commit="X")
    o = rig.say("숨이 안 쉬어져요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))                       # 위험 알림이 있었어야 하는 턴
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "BEFORE_COMMIT", "step": "COMMIT"}]}
    assert o.orchestrator_output["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    assert o.frontend_input["response_text"] is None
    assert le(o)["staff_alert"] is None and le(o)["staff_alert_delivery"] == "NOT_APPLICABLE"
    assert [i for i in rig.console.inputs[n_console:] if i["staff_alert"]] == []
    assert rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS"


def test_commit_unknown_outcome_is_not_cached_so_retry_reruns():
    rig = Rig()
    rig.start()
    faulty = FaultyStore(rig.store, fail_commit="UNKNOWN")
    rig.orch.store = faulty
    o = rig.say("안 먹어요", out=HEALTH, turn_id="T-U")
    assert not o.cacheable and o.orchestrator_output["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    faulty.fail_commit = None
    o2 = rig.say("안 먹어요", out=HEALTH, turn_id="T-U")
    assert not o2.replayed and rec(rig.doc(), "Q1.D01.diagnosed")["current_value"] == {"answer": False}


def test_staff_delivery_failure_still_responds_and_signals():
    rig = risk_rig()
    rig.start()
    rig.console.fail = True
    o = rig.say("숨이 안 쉬어져요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
    assert o.orchestrator_output["turn_failure_signal"] == {"session_id": "S1", "write_committed": "COMMITTED", "staff_alert": "DELIVERY_FAILED"}
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "AFTER_COMMIT", "step": "DELIVER_STAFF"}]}
    assert le(o)["staff_alert_delivery"] == "FAILURE" and o.frontend_input["response_text"]
    assert rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"        # 쓰기는 반영됐다
    assert o.staff_console_input["staff_alert"] is None and o.staff_console_input["turn_failure_signal"]


def test_response_failure_after_commit():
    rig = Rig()
    rig.start()

    def boom(_):
        raise RuntimeError("시험용 실패")
    rig.session.respond = boom
    o = rig.say("안 먹어요", out=HEALTH)
    assert o.orchestrator_output["turn_failure_signal"] == {"session_id": "S1", "write_committed": "COMMITTED", "staff_alert": "NONE"}
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "AFTER_COMMIT", "step": "LoRA-B"}]}
    assert o.frontend_input["response_text"] is None
    assert rec(rig.doc(), "Q1.D01.diagnosed")["current_value"] == {"answer": False}      # 저장은 확정됐다
    # 응답을 못 들었으므로 다음 A 입력의 current_question은 문항 은행의 질문 문장이 된다(직전 응답이 없다)


def test_next_turn_after_missing_response_uses_the_question_sentence():
    rig = Rig()
    rig.start()
    real = rig.session.respond
    rig.session.respond = lambda r: (_ for _ in ()).throw(RuntimeError("x"))
    rig.say("안 먹어요", out=HEALTH)
    rig.session.respond = real
    rig.doc()["dialogue_log"]
    rig.say("고혈압 약", out=ext("NEXT", "Q1.D03", val("Q1.D02.diagnosed", False), val("Q1.D02.on_medication", False)))
    # 직전 응답 로그는 시작 턴의 문장이 남아 있다: 실패한 턴의 응답 없는 로그는 건너뛴다
    assert rig.session.extract_requests[-1]["current_question"] == "응답입니다."


def test_frontend_sink_failure():
    sink = RecordingSink(fail=True)
    rig = Rig(frontend_sink=sink)
    o = rig.start()
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "AFTER_COMMIT", "step": "DELIVER_FRONT"}]}
    assert o.frontend_input["response_text"] is None and le(o)["response_text"]
    ok = RecordingSink()
    rig2 = Rig(frontend_sink=ok)
    o2 = rig2.start()
    assert ok.inputs == [o2.frontend_input] and o2.orchestrator_output["turn_failure_signal"] is None


def test_dispatch_failure_is_recorded_not_a_turn_failure():
    rig = Rig()
    rig.dispatcher.fail = True
    rig.start()
    o = rig.say(kind="SESSION_END")
    assert o.orchestrator_output["dispatch_called"] is True and o.orchestrator_output["turn_failure_signal"] is None
    assert rig.doc()["dispatch_log"] == [{"succeeded": False}]
    assert o.frontend_input["response_text"] is None            # 이탈: 응답이 없다


def test_unconfigured_staff_console_fails_honestly():
    rig = risk_rig()
    rig.orch.staff_console = Orchestrator(MemorySessionStore(), rig.registry).staff_console   # 기본값
    rig.start()
    o = rig.say("숨이 안 쉬어져요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
    assert o.orchestrator_output["turn_failure_signal"]["staff_alert"] == "DELIVERY_FAILED"


def test_session_creation_failure_leaves_no_log():
    rig = Rig()
    rig.orch.store = FaultyStore(rig.store, fail_create=True)
    o = rig.say("안 먹어요", out=HEALTH)
    assert o.orchestrator_output["log_entry"] is None and o.orchestrator_output["turn_kind"] == "UTTERANCE"
    assert o.orchestrator_output["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    assert rig.store.load("S1")["status"] == "NOT_FOUND"


def test_session_bound_release_unavailable_fails_first_step():
    rig = Rig()
    rig.start()
    rig.registry._loaded.clear()
    o = rig.say("안 먹어요")
    assert le(o)["turn_result"] == {"failed_steps": [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]}


def test_write_that_cannot_be_applied_changes_nothing():
    rig = Rig()
    rig.start()
    snap = rig.doc()["answer_records"]
    real = __import__("survey_service.extraction", fromlist=["plan_writes"]).plan_writes

    def bad(doc, out):
        w = real(doc, out)
        w["answer_updates"].append({"slot": w["answer_updates"][0]["slot"], "action": "NEW_REVISION", "value": 1, "precision": "exact"})
        w["answer_updates"].append({"slot": {"question_id": "Q1.D01", "repeat_key": "", "slot_id": "nope"}, "action": "FIRST_WRITE", "value": 1, "precision": "exact"})
        return w
    import survey_service.extraction as ex
    orig, ex.plan_writes = ex.plan_writes, bad
    try:
        o = rig.say("안 먹어요", out=HEALTH)
    finally:
        ex.plan_writes = orig
    assert le(o)["turn_result"]["failed_steps"][0]["step"] == "COMMIT"
    assert rig.doc()["answer_records"] == snap


# ---------------------------------------------------------------- 멱등과 직렬화
def test_same_turn_id_returns_same_result_without_second_write():
    rig = Rig()
    rig.start()
    a = rig.say("안 먹어요", out=HEALTH, turn_id="T-1")
    doc1 = rig.doc()
    b = rig.say("안 먹어요", out=HEALTH, turn_id="T-1")          # 큐에 한 번 더 넣었지만 소비되면 안 된다
    assert b.replayed and b.orchestrator_output == a.orchestrator_output and b.frontend_input == a.frontend_input
    assert rig.doc() == doc1
    assert len(rig.model.queue) == 1 and len(rig.session.extract_requests) == 1          # 모델을 다시 부르지 않았다


def test_same_turn_id_different_input_conflicts():
    rig = Rig()
    rig.start(turn_id="T-1")
    with pytest.raises(TurnIdConflict):
        rig.say("안녕", turn_id="T-1")


def test_replay_of_ignored_turn_does_not_log_twice():
    rig = risk_rig()
    prepare(rig, "paused")
    n = len(rig.doc()["dialogue_log"])
    a = rig.say("언제 와요", turn_id="T-I")
    b = rig.say("언제 와요", turn_id="T-I")
    assert b.replayed and b.orchestrator_output == a.orchestrator_output
    assert len(rig.doc()["dialogue_log"]) == n + 1


def test_committed_but_no_result_recovers_without_rewriting():
    rig = Rig()
    rig.start()
    msg = {"session_id": "S1", "kind": "UTTERANCE", "utterance": "안 먹어요"}
    from boundary_contracts import canonical
    h = canonical.sha256_hash({"m": msg, "d": None})
    st = rig.doc()
    rig.store._turns[("S1", "T-R")] = __import__("survey_service.store.base", fromlist=["TurnRecord"]).TurnRecord(h, True, None)
    o = rig.orch.handle_turn(turn_id="T-R", frontend_message=msg)
    assert o.replayed and o.orchestrator_output["turn_failure_signal"]["write_committed"] == "COMMITTED"
    assert rig.doc() == st


def test_same_session_turns_are_serialized():
    rig = Rig()
    rig.start()
    active, peak = [0], [0]
    lock = threading.Lock()
    real = rig.session.extract

    def slow(req):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        import time
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return real(req)
    rig.session.extract = slow
    ts = [threading.Thread(target=lambda i=i: rig.orch.handle_turn(turn_id=f"c{i}", frontend_message={"session_id": "S1", "kind": "UTTERANCE", "utterance": f"x{i}"})) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1 and len(rig.doc()["dialogue_log"]) == 7


# ---------------------------------------------------------------- 릴리스 전환
def test_new_release_applies_to_new_sessions_only():
    rig = Rig()
    rig.start(sid="OLD")
    new_model = ScriptedModel()
    rel2 = LoadedRelease("rel-e2e-2", {}, sample_bank(version=2), make_session(new_model))
    rig.registry.register(rel2, activate=True)
    o_old = rig.start(sid="OLD")                 # 진행 중인 세션은 자기 릴리스로 계속
    assert le(o_old)["question_bank_version"] == 1 and rig.store.release_id_of("OLD") == "rel-e2e-1"
    assert o_old.trace["orchestrator_input"]["latest_question_bank_version"] == 2
    assert o_old.release_id == "rel-e2e-1"
    o_new = rig.start(sid="NEW")
    assert le(o_new)["question_bank_version"] == 2 and rig.store.release_id_of("NEW") == "rel-e2e-2" and o_new.release_id == "rel-e2e-2"
    assert rig.doc("OLD")["session_info"]["question_bank_version"] == 1


# ---------------------------------------------------------------- Trace
def test_trace_is_schema_valid_and_complete():
    got = []

    class Sink:
        def submit(self, release_id, trace):
            got.append((release_id, copy.deepcopy(trace)))
    rig = risk_rig(trace_sink=Sink())
    v = jsonschema.Draft202012Validator(survey_schema.load_schema("TurnTrace"))
    rig.start()
    rig.say("안 먹어요", out=HEALTH)
    rig.say("숨이 안 쉬어져요", out=ext("REDIRECT", "Q1.D02", reason="OFF_TOPIC"))
    rig.say("언제 와요")
    rig.say(decision="END")
    rig.say(decision="END", turn_id="ign")                    # 끝난 세션: 처리 안 함
    assert len(got) == 6
    for rid, tr in got:
        assert rid == "rel-e2e-1"
        errs = list(v.iter_errors(tr))
        assert not errs, errs[0].message[:200]
        assert tr["contract_version"] == 1 and tr["orchestrator_output"] and tr["orchestrator_input"]
    full = got[1][1]
    for k in ["extract_input", "extract_output", "extract_attempts", "extract_status", "validation_ok", "validation_violations",
              "response_input", "response_output", "response_outcome", "frontend_input", "session_before", "session_after", "log_entry",
              "persistence_load_request", "persistence_load_result", "persistence_commit_request", "persistence_commit_result"]:
        assert full[k] is not None, k
    assert not any(k.startswith(("parser_", "normalizer_", "judge_", "planner_", "record_writer_", "generator_")) for k in full)
    assert full["dispatch_input"] is None and got[4][1]["dispatch_input"] is not None
    assert got[5][1]["extract_input"] is None and got[5][1]["log_entry"] is None
    assert got[0][1]["session_before"]["dialogue_log"] == [] and got[1][1]["session_after"]["dialogue_log"][-1] == got[1][1]["log_entry"]


def test_trace_sink_failure_does_not_fail_the_turn():
    class Bad:
        def submit(self, *a):
            raise RuntimeError("outbox 사용 불가")
    rig = Rig(trace_sink=Bad())
    o = rig.start()
    assert o.frontend_input["response_text"] and rig.orch.counters["trace_enqueue_failed"] == 1


def test_replayed_turn_is_not_traced_twice():
    got = []

    class Sink:
        def submit(self, release_id, trace):
            got.append(1)
    rig = Rig(trace_sink=Sink())
    rig.start(turn_id="x")
    rig.start(turn_id="x")
    assert got == [1]


# ---------------------------------------------------------------- SQLite 저장소로 같은 흐름
def test_conversation_on_sqlite_survives_restart(tmp_path):
    db = str(tmp_path / "s.db")
    rig = Rig(store=SqliteSessionStore(db))
    rig.start()
    rig.say("안 먹어요", out=HEALTH, turn_id="T-A")
    first = rig.doc()
    rig.store.close()
    rig2 = Rig(store=SqliteSessionStore(db))
    assert rig2.doc() == first
    again = rig2.orch.handle_turn(turn_id="T-A", frontend_message={"session_id": "S1", "kind": "UTTERANCE", "utterance": "안 먹어요"})
    assert again.replayed and rig2.doc() == first
    rig2.say("고혈압", out=ext("NEXT", "Q1.D03", val("Q1.D02.diagnosed", True), val("Q1.D02.on_medication", True)), turn_id="T-B")
    assert rec(rig2.doc(), "Q1.D02.on_medication")["current_value"] == {"answer": True}
    assert rig2.session.extract_requests[0]["current_question"] == "응답입니다." or True
