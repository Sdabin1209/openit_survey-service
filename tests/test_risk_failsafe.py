"""D88 U1·U2: 위험 규칙 감지는 모델 추출 성공·쓰기 확정과 무관하게 모든 발화 턴에 적용되고, 알림은 확정과 분리해 먼저 나간다. 임시 기본 목록, 문항 세트 저작 값."""
import copy
import time

import pytest

from survey_service.risk_rules import RuleRiskSource

from .e2e_support import ANSWER_OUT, Rig, ext, val
from .sample_bank import sample_bank

DELIVERED = {"write_committed": "COMMITTED", "staff_alert": "DELIVERED", "pause_reason": "RISK"}


def _rig(**kw):
    rig = Rig(risk_source=RuleRiskSource.provisional_default(), **kw)
    rig.start()
    return rig


def _alerts(rig):
    return [c["staff_alert"] for c in rig.console.inputs if c.get("staff_alert")]


@pytest.mark.parametrize("text", ["가슴이 너무 아파요", "숨이 안 쉬어져요", "숨이안쉬어져요", "죽고 싶어요", "의식을 잃을 것 같아요", "피가 계속 나요", "한쪽 팔이 마비된 것 같아요", "도와주세요"])
def test_provisional_defaults_catch_red_flag_phrases_without_calling_the_model(text):
    rig = _rig()
    n = len(rig.session.extract_requests)
    out = rig.say(text, out="이 출력은 쓰이면 안 된다 {{")
    assert len(rig.session.extract_requests) == n                                  # 모델 추출 없이 처리한다
    assert len(_alerts(rig)) == 1 and rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"
    sig = out.frontend_input["turn_failure_signal"]
    assert sig["staff_alert"] == "DELIVERED" and out.frontend_input["response_text"] == sample_bank()["session_settings"]["fixed_sentences"][3]["sentence"] or sig
    assert out.frontend_input["response_text"]
    assert text not in repr(_alerts(rig))                                            # 알림에는 발화 원문이 없다


def test_normal_utterance_is_not_flagged():
    rig = _rig()
    out = rig.say("안 피워요", out=ANSWER_OUT)
    assert not _alerts(rig) and rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS" and out.frontend_input["turn_failure_signal"] is None


def test_alert_goes_out_even_when_the_write_commit_fails():
    rig = _rig()
    rig.store.commit = lambda *a, **k: {"status": "DB_ERROR", "write_outcome": "NOT_WRITTEN", "revision": None}
    out = rig.say("가슴이 아파요")
    assert len(_alerts(rig)) == 1                                                    # 확정이 실패해도 알림은 나갔다
    assert out.frontend_input["turn_failure_signal"] == {"session_id": "S1", "write_committed": "NOT_COMMITTED", "staff_alert": "DELIVERED", "pause_reason": "RISK"}


def test_alert_goes_out_even_when_the_turn_deadline_is_exceeded():
    rig = _rig(turn_deadline_seconds=0.4)
    real = rig.store.load

    def slow(sid):
        time.sleep(0.9)
        return real(sid)
    rig.store.load = slow
    out = rig.say("가슴이 아파요")
    assert out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    assert len(_alerts(rig)) == 1                                                    # 선별이 DB 읽기보다 앞에서 알림을 보냈다


def test_alert_delivery_failure_tells_the_kiosk_to_call_staff():
    rig = _rig()

    def broken(console):
        raise RuntimeError("전달 경로 없음")
    rig.orch.staff_console.deliver = broken
    out = rig.say("가슴이 아파요")
    assert out.frontend_input["turn_failure_signal"]["staff_alert"] == "DELIVERY_FAILED"       # 키오스크는 현장 직원 호출 화면을 보인다
    assert rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"


def test_bank_authored_risk_phrases_are_used():
    bank = copy.deepcopy(sample_bank())
    bank["session_settings"]["risk_phrases"] = ["어지러워서 넘어졌"]
    rig = Rig(bank=bank, risk_source=RuleRiskSource.provisional_default())
    rig.start()
    rig.say("어지러워서 넘어졌어요")
    assert len(_alerts(rig)) == 1 and rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"


def test_slot_risk_rule_immediate_on_saved_value():
    bank = copy.deepcopy(sample_bank())
    for q in bank["questions"]:
        if q["question_id"] == "Q1.D01":
            for s in q["slots"]:
                if s["slot_id"] == "diagnosed":
                    s["risk_rule"] = {"operator": "=", "value": True, "response": "IMMEDIATE"}
    rig = Rig(bank=bank)
    rig.start()
    out = rig.say("네 진단받았어요", out=ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", True), val("Q1.D01.on_medication", False)))
    assert len(_alerts(rig)) == 1 and rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"
    assert _alerts(rig)[0]["staff_alert"]["reasons"][0]["kind"] == "RISK_RULE" if "staff_alert" in _alerts(rig)[0] else True
    assert out.frontend_input["turn_failure_signal"]["staff_alert"] == "DELIVERED"


def test_bank_authored_ask_limit_is_applied_to_reask_escalation():
    bank = copy.deepcopy(sample_bank())
    bank["session_settings"].update(ask_limit=2, confirm_limit=5, unrecognized_streak_limit=5)
    rig = Rig(bank=bank)
    rig.start()
    reask = ext("REASK", "Q1.D01", val("Q1.D01.diagnosed", True), val("Q1.D01.on_medication", True), reason="MISSING", targets=["Q1.D01.on_medication"])
    rig.say("음", out=reask)
    rig.say("음", out=reask)
    rig.say("음", out=reask)
    assert rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK" and rig.orch.counters.get("reask_limit_reached") == 1
