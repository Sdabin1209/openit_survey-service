"""U34: 되묻기 연속 상한(ADR-0020 + ADR-0032)은 REASK·CONFIRM에 더해 REDIRECT와 실패 턴(추출 실패·검증 위반)도 센다. 상한 값(기본 3)·정지 방식은 그대로다.
실패 턴 재전송(같은 turn_id)은 한 번만 센다(세션 시각 기록 session_timeline의 turn_id로 거른다)."""
import pytest

from survey_service.alerts import AlertStore

from .e2e_support import Rig, ext, val
from .test_reask_limit import SENT, _bank_without_authored_limits, _kinds

REASK = ext("REASK", "Q1.D01", val("Q1.D01.on_medication", None, "MISSING"), reason="MISSING", targets=["Q1.D01.on_medication"])
REDIRECT = ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC")
NEXT = ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", False), val("Q1.D01.on_medication", False))
BROKEN = "깨진 출력 {{"


@pytest.fixture
def rig(tmp_path):
    r = Rig(bank=_bank_without_authored_limits(), max_consecutive_reask=3, timeline=AlertStore(str(tmp_path / "a.db")))
    r.start()
    yield r


def fail(rig, turn_id=None):
    return rig.say("음", out=BROKEN, times=6, turn_id=turn_id)           # 서버 안 재시도 3 x 서비스 재시도 2


def called(rig):
    return _kinds(rig) == ["CALL_STAFF"] and rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"


def test_redirect_streak_calls_staff_on_the_fourth(rig):
    for _ in range(3):
        assert rig.say("음", out=REDIRECT).frontend_input["response_text"] != SENT
    assert _kinds(rig) == []
    assert rig.say("음", out=REDIRECT).frontend_input["response_text"] == SENT and called(rig)


def test_failed_turn_streak_calls_staff_on_the_fourth(rig):
    for _ in range(3):
        assert fail(rig).orchestrator_output["turn_failure_signal"] is not None
    assert _kinds(rig) == []
    o = fail(rig)
    assert o.frontend_input["response_text"] == SENT and called(rig)      # 같은 직원 호출 경로: 정지 + CALL_STAFF 알림 + 고정 문장


def test_validation_violation_failures_count_too(rig):
    bad = ext("NEXT", "Q1.D02", val("Q1.D01.shoe_size", 270))            # 문항 세트에 없는 칸: 서비스 검사 위반 두 번이면 턴 실패
    for _ in range(3):
        rig.say("음", out=bad, times=2)
    assert _kinds(rig) == []
    assert rig.say("음", out=bad, times=2).frontend_input["response_text"] == SENT and called(rig)


def test_mixed_redirect_reask_and_failures_count_together(rig):
    rig.say("음", out=REDIRECT)
    rig.say("음", out=REASK)
    fail(rig)
    assert _kinds(rig) == []
    assert rig.say("음", out=REDIRECT).frontend_input["response_text"] == SENT and called(rig)


def test_a_next_in_between_resets_the_count(rig):
    for _ in range(3):
        rig.say("음", out=REDIRECT)
    rig.say("음", out=NEXT)                                              # 정상 진행: 연속이 끊긴다
    for _ in range(3):
        rig.say("음", out=REDIRECT)
    assert _kinds(rig) == [] and rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS"


def test_resending_a_failed_turn_id_is_counted_once(rig):
    fail(rig, "f1")
    fail(rig, "f2")
    fail(rig, "f2")                                                      # 키오스크가 실패한 턴을 같은 turn_id로 다시 보낸다
    fail(rig, "f2")
    assert _kinds(rig) == []                                             # 서로 다른 실패는 2개뿐
    assert fail(rig, "f3").orchestrator_output["turn_failure_signal"] is not None and _kinds(rig) == []      # 3번째 실패(앞선 서로 다른 실패 2개 + 이번)는 아직 상한 아래
    assert fail(rig, "f4").frontend_input["response_text"] == SENT and called(rig)                      # 앞선 서로 다른 실패 3개 뒤의 4번째


def test_resending_the_escalating_turn_does_not_call_staff_twice(rig):
    for _ in range(3):
        rig.say("음", out=REDIRECT)
    first = rig.say("음", out=REDIRECT, turn_id="esc")
    again = rig.orch.handle_turn(turn_id="esc", frontend_message={"session_id": "S1", "kind": "UTTERANCE", "utterance": "음"})
    assert first.frontend_input["response_text"] == SENT and again.replayed and _kinds(rig) == ["CALL_STAFF"]
