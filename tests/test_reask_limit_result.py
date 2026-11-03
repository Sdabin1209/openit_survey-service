"""규칙 D95 U36. U36·G12·U35: 되묻기 한도에 닿아 빈칸으로 둔 문항이 결과 조회(SessionResult.skipped_details)에 사유 REASK_LIMIT로 나온다 (ADR-0054)."""
from .e2e_support import ext, val
from .test_results_signals_retention import env  # noqa: F401
from .test_skipped_details_u35 import result, unclear


def test_limit_blank_is_reported_with_reason_reask_limit_per_slot_and_session_goes_on(env):
    env.start("K1")
    for i in range(2):                                                            # 이 문항 세트의 저작 한도 2(confirm_limit 등, 서비스 기본 3과의 최솟값): 두 번은 되묻는다
        env.say("K1", "여덟 일이요", f"t{i}", unclear("Q1.D01.diagnosed"))
    assert result(env)["skipped_details"] == [{"item": "Q1.D01", "slot": "Q1.D01.diagnosed", "reason": "UNCLEAR"}]
    env.say("K1", "여덟 일이요", "t2", unclear("Q1.D01.diagnosed"))                # 세 번째: 빈칸으로 두고 다음 문항
    r = result(env)
    assert r["skipped_details"] == [{"item": "Q1.D01", "slot": "Q1.D01.diagnosed", "reason": "REASK_LIMIT"}]
    assert r["status"] == "IN_PROGRESS"                                           # 정지하지 않았다
    env.say("K1", "아니요", "t3", ext("NEXT", "Q1.D03", val("Q1.D02.diagnosed", False), val("Q1.D02.on_medication", False)))
    assert result(env)["skipped_details"] == [{"item": "Q1.D01", "slot": "Q1.D01.diagnosed", "reason": "REASK_LIMIT"}]      # 다음 문항 진행이 사유를 지우지 않는다
