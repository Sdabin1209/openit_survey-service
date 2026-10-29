"""U24(R7): 서버(model-runtime)가 fields로 자료형·범위·선택지를 보증하므로 서비스는 다시 검사하지 않고(VALUE_INVALID, VALUE_TYPE_MISMATCH 삭제),
서버 규칙으로 덮이지 않는 검사만 남는다: 칸이 문항 은행에 있는가(UNKNOWN_SLOT), 현재 문항 밖 칸 저장(OUT_OF_CURRENT_ITEM). 계약 3.0.0부터 칸 규격이 없는 문항 은행은 적재를 거부하므로(ADR-0043) 규격 없는 칸의 값 검사도 없다."""
import copy

import pytest

from .e2e_support import Rig, ext, val
from .sample_bank import sample_bank
from .test_reask_limit import _bank_without_authored_limits, _kinds


def _fresh(bank):
    for k in ("ask_limit", "confirm_limit", "unrecognized_streak_limit"):
        bank["session_settings"].pop(k, None)       # 연속 실패 상한(ADR-0032)과 섞이지 않게
    return bank


BAD_VALUES = [("on_medication", "아무거나"), ("diagnosed", 123456789), ("diagnosed", ""), ("diagnosed", "x" * 500)]


@pytest.mark.parametrize("slot,value", BAD_VALUES)
def test_typed_bank_trusts_the_server_for_value_type_and_range(slot, value):
    rig = Rig(bank=_fresh(sample_bank()))
    rig.start()
    o = rig.say("말", out=ext("NEXT", "Q1.D02", val(f"Q1.D01.{slot}", value)))
    assert len(rig.session.extract_requests) == 1 and o.trace["extract_attempts"] == 1       # 재검사도 재호출도 없다
    assert o.orchestrator_output["turn_failure_signal"] is None and not (o.trace["validation_violations"] or [])


def test_checks_the_server_cannot_do_remain_for_a_typed_bank():
    rig = Rig(bank=_fresh(sample_bank()))
    rig.start()
    o = rig.say("말", out=ext("NEXT", "Q1.D02", val("Q1.D01.shoe_size", 270)), times=2)             # 문항 은행에 없는 칸: 서버는 flow 문항만 안다
    assert [v["code"] for v in o.trace["validation_violations"]] == ["UNKNOWN_SLOT"]
    rig.say("네", out=ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", False), val("Q1.D01.on_medication", False)))
    o = rig.say("아무 말", out=ext("NEXT", "Q1.D03", val("Q4.answer", True)), times=2)             # 현재 문항이 아닌 칸을 정정 아닌데 저장
    assert [v["code"] for v in o.trace["validation_violations"]] == ["OUT_OF_CURRENT_ITEM"]
