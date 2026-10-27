"""G12: REASK·CONFIRM 연속 상한 메커니즘(SURVEY_MAX_CONSECUTIVE_REASK). 상한 값은 정해지지 않았다(CR-012). 시험은 임의의 값 2를 쓴다."""
from survey_service.settings import Settings

from .e2e_support import Rig, ext, val

REASK = ext("REASK", "Q1.D01", val("Q1.D01.on_medication", None, "MISSING"), reason="MISSING", targets=["Q1.D01.on_medication"])
OTHER = ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC")
SENT = "직원을 불러 드리겠습니다. 잠시만 기다려 주세요."          # 문항 세트의 CALL_STAFF 고정 문장(새 문구 없음)


def _kinds(rig):
    return [c["staff_alert"]["staff_alert"]["kind"] for c in rig.console.inputs if c.get("staff_alert")]


def test_default_is_off_and_never_escalates():
    assert Settings().max_consecutive_reask == 0 and Settings.from_env({"SURVEY_RELEASE_READER": "x", "SURVEY_TRACE_INGEST": "x", "SURVEY_MODEL_BACKEND": "fake"}).max_consecutive_reask == 0
    rig = Rig()
    rig.start()
    for _ in range(8):
        o = rig.say("음", out=REASK)
        assert o.frontend_input["response_text"] != SENT
    assert rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS" and _kinds(rig) == []


def test_escalates_to_existing_staff_call_path_after_cap():
    rig = Rig(max_consecutive_reask=2)
    rig.start()
    first = ext("REASK", "Q1.D01", val("Q1.D01.diagnosed", True), val("Q1.D01.on_medication", True), reason="MISSING", targets=["Q1.D01.on_medication"])
    confirm = ext("CONFIRM", "Q1.D01", val("Q1.D01.on_medication", False, "CONFLICT"), reason="CONFLICT_WITH_PREVIOUS", targets=["Q1.D01.on_medication"])
    for out in (first, confirm):                                 # 상한까지는 되묻는다(REASK와 CONFIRM이 함께 센다)
        o = rig.say("음", out=out)
        assert o.frontend_input["response_text"] != SENT
    assert _kinds(rig) == []
    o = rig.say("음", out=REASK)
    assert o.frontend_input["response_text"] == SENT              # 기존 문항 세트 고정 문장
    assert _kinds(rig) == ["CALL_STAFF"]                          # 기존 직원 알림 경로
    doc = rig.doc()
    assert doc["progress_record"]["session_status"] == "PAUSED_RISK"      # 기존 정지 신호(키오스크 state=PAUSED_RISK)
    assert rig.orch.counters["reask_limit_reached"] == 1
    rig.say(decision="RESUME")                                   # 직원이 풀면 다시 진행, 연속 횟수는 다시 센다
    assert rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS"
    for _ in range(2):
        assert rig.say("음", out=REASK).frontend_input["response_text"] != SENT
    assert _kinds(rig) == ["CALL_STAFF"]


def test_other_action_breaks_the_streak():
    rig = Rig(max_consecutive_reask=2)
    rig.start()
    for _ in range(5):
        rig.say("음", out=REASK)
        rig.say("음", out=OTHER)
    assert _kinds(rig) == [] and rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS"


def test_streak_is_counted_from_session_state_so_it_survives_restart():
    rig = Rig(max_consecutive_reask=2)
    rig.start()
    rig.say("음", out=REASK)
    rig.say("음", out=REASK)
    # 새 Orchestrator, 같은 저장소: 카운터는 세션 문서에서 나온다
    rig2 = Rig(store=rig.store, max_consecutive_reask=2)
    rig2.model.queue.append(REASK)
    o = rig2.orch.handle_turn(turn_id="x1", frontend_message={"session_id": "S1", "kind": "UTTERANCE", "utterance": "음"})
    assert o.frontend_input["response_text"] == SENT
