"""규칙 D95 U36 (되묻기 상한 3 뒤 스킵+사유). G12: REASK·CONFIRM(U34부터 REDIRECT와 실패 턴도) 연속 상한(SURVEY_MAX_CONSECUTIVE_REASK, 기본 3, ADR-0020, ADR-0032). 연속은 문항과 무관하게 이어진 턴 수이고 다른 action이 나오면 끊긴다.
상한 동작 시험은 값 2를, 기본값 N=3의 경계 시험은 3을 쓴다."""
from survey_service.settings import Settings

import copy

from .e2e_support import Rig, ext, val
from .sample_bank import sample_bank

REASK = ext("REASK", "Q1.D01", val("Q1.D01.on_medication", None, "MISSING"), reason="MISSING", targets=["Q1.D01.on_medication"])
OTHER = ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", False), val("Q1.D01.on_medication", False))       # 연속을 끊는 정상 진행(REDIRECT는 U34부터 연속에 센다, ADR-0032)
SENT = "직원을 불러 드리겠습니다. 잠시만 기다려 주세요."          # 문항 세트의 CALL_STAFF 고정 문장(새 문구 없음)


def _kinds(rig):
    return [c["staff_alert"]["staff_alert"]["kind"] for c in rig.console.inputs if c.get("staff_alert")]


def test_zero_default_is_not_the_service_default_and_cap_off_never_skips():
    base = {"SURVEY_RELEASE_READER": "tests.fakes:release_reader", "SURVEY_TRACE_INGEST": "tests.fakes:trace_ingest", "SURVEY_MODEL_BACKEND": "fake"}
    assert Settings().max_consecutive_reask == 3 and Settings.from_env(base).max_consecutive_reask == 3         # 기본 3(D88 U10), 끄려면 0을 명시한다
    assert Settings.from_env({**base, "SURVEY_MAX_CONSECUTIVE_REASK": "0"}).max_consecutive_reask == 0
    bank = copy.deepcopy(sample_bank())
    for k in ("ask_limit", "confirm_limit", "unrecognized_streak_limit"):
        bank["session_settings"][k] = 99              # 문항 세트가 상한을 저작하지 않았고 서비스 상한도 끈 경우(Orchestrator 기본 0)
    rig = Rig(bank=bank)
    rig.start()
    for _ in range(8):
        o = rig.say("음", out=REASK)
        assert o.frontend_input["response_text"] != SENT
    assert rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS" and _kinds(rig) == []


def test_blanks_the_item_and_moves_on_after_cap():
    """사용자 결정(U36·G12·U35): 한도에 닿으면 직원을 부르지 않고 그 문항을 빈칸으로 두고 다음 문항으로 간다(ADR-0054)."""
    rig = Rig(max_consecutive_reask=2)
    rig.start()
    first = ext("REASK", "Q1.D01", val("Q1.D01.diagnosed", True), val("Q1.D01.on_medication", True), reason="MISSING", targets=["Q1.D01.on_medication"])
    confirm = ext("CONFIRM", "Q1.D01", val("Q1.D01.on_medication", False, "CONFLICT"), reason="CONFLICT_WITH_PREVIOUS", targets=["Q1.D01.on_medication"])
    for out in (first, confirm):                                 # 상한까지는 되묻는다(REASK와 CONFIRM이 함께 센다)
        o = rig.say("음", out=out)
        assert o.frontend_input["response_text"] != SENT
    assert rig.doc()["progress_record"]["current_question_id"] == "Q1.D01"
    o = rig.say("음", out=REASK)                                 # 한도 뒤의 같은 되묻기 -> 빈칸으로 두고 다음 문항
    prog = rig.doc()["progress_record"]
    assert o.frontend_input["response_text"] != SENT and o.frontend_input["turn_failure_signal"] is None
    assert prog["session_status"] == "IN_PROGRESS" and prog["current_question_id"] == "Q1.D02"
    assert {"question_id": "Q1.D01", "reason": "REASK_LIMIT"} in prog["skipped"]
    assert _kinds(rig) == [] and rig.orch.counters["reask_limit_blank"] == 1 and "reask_limit_reached" not in rig.orch.counters      # 직원 호출 경로는 없다
    assert rig.orch.blank_details(rig.doc()) == [{"item": "Q1.D01", "reason": "REASK_LIMIT"}]      # 칸 원인이 정해진 값(UNCLEAR 등)이 없었으므로 문항만 적는다
    for _ in range(2):                                           # 다음 문항에서 연속은 다시 센다(SKIP이 연속을 끊었다)
        assert rig.say("음", out=ext("REASK", "Q1.D02", val("Q1.D02.on_medication", None, "MISSING"), reason="MISSING", targets=["Q1.D02.on_medication"])).frontend_input["turn_failure_signal"] is None
    assert rig.doc()["progress_record"]["current_question_id"] == "Q1.D02"


def test_the_last_item_blank_ends_the_survey_without_staff():
    rig = Rig(max_consecutive_reask=1)
    rig.start()
    rig.say("음", out=ext("NEXT", "Q10", val("Q9_2.duration_minutes", 5)))
    rig.say("음", out=ext("REASK", "Q10", val("Q10.days", None, "MISSING"), reason="MISSING", targets=["Q10.days"]))
    rig.say("음", out=ext("REASK", "Q10", val("Q10.days", None, "MISSING"), reason="MISSING", targets=["Q10.days"]))
    prog = rig.doc()["progress_record"]
    assert prog["current_question_id"] == "END" and prog["session_status"] == "ENDED_COMPLETE" and _kinds(rig) == []


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
    rig2.orch.handle_turn(turn_id="x1", frontend_message={"session_id": "S1", "kind": "UTTERANCE", "utterance": "음"})
    assert rig2.doc()["progress_record"]["current_question_id"] == "Q1.D02"


def _bank_without_authored_limits():
    bank = copy.deepcopy(sample_bank())
    for k in ("ask_limit", "confirm_limit", "unrecognized_streak_limit"):
        bank["session_settings"][k] = 99          # 서비스 상한만 시험한다(저작값이 있으면 최솟값이 이긴다, ADR-0014)
    return bank


def test_default_cap_3_allows_three_then_blanks_the_item_on_the_fourth():
    cap = Settings().max_consecutive_reask
    assert cap == 3
    rig = Rig(bank=_bank_without_authored_limits(), max_consecutive_reask=cap)
    rig.start()
    for _ in range(3):                                           # N번째까지는 정상으로 되묻는다
        assert rig.say("음", out=REASK).frontend_input["response_text"] != SENT
    assert _kinds(rig) == [] and rig.doc()["progress_record"]["current_question_id"] == "Q1.D01"
    rig.say("음", out=REASK)                                     # N+1번째에 빈칸 + 다음 문항
    assert _kinds(rig) == [] and rig.doc()["progress_record"]["current_question_id"] == "Q1.D02" and rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS"


def test_other_action_resets_the_counter_for_cap_3():
    rig = Rig(bank=_bank_without_authored_limits(), max_consecutive_reask=3)
    rig.start()
    for _ in range(3):
        rig.say("음", out=REASK)
    rig.say("음", out=OTHER)                                     # 다른 action이 나오면 리셋
    for _ in range(3):
        assert rig.say("음", out=REASK).frontend_input["response_text"] != SENT
    assert _kinds(rig) == []


def test_zero_turns_the_cap_off():
    rig = Rig(bank=_bank_without_authored_limits(), max_consecutive_reask=0)
    rig.start()
    for _ in range(10):
        assert rig.say("음", out=REASK).frontend_input["response_text"] != SENT
    assert _kinds(rig) == []
