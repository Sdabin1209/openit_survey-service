"""U35: SessionResult에 빈칸의 원인을 남긴다. `skipped`(문항 ID 문자열 목록)는 그대로 두고 `skipped_details: [{item, slot?, reason}]`를 더한다(추가형, survey-schema 1.3.0, ADR-0036).
원인 코드는 UNKNOWN(모름: A 출력 status UNKNOWN, HOLD/UNCERTAIN), REJECTED(거절·보류: status REJECTED, SKIP/REFUSED·REQUEST_SKIP), UNCLEAR(불분명·범위 밖: status UNCLEAR, REASK의 OUT_OF_RANGE 등).
대응은 학습 데이터셋 labels(val 3221행)에서 확인했다. 환자 발화 원문은 넣지 않는다(감사 C2)."""
import json

from .e2e_support import ext, val
from .test_results_signals_retention import RESULT, RESULT_V, env  # noqa: F401


def result(env, sid="K1"):
    st, body, _ = env.call("GET", f"/v1/results/{sid}", auth=RESULT)
    assert st == 200, (st, body)
    assert not list(RESULT_V.iter_errors(body)), list(RESULT_V.iter_errors(body))
    return body


def hold(*keys):
    return ext("HOLD", "Q1.D02", *[val(k, None, "UNKNOWN", "unspecified") for k in keys], reason="UNCERTAIN", targets=list(keys))


def skip(reason, *keys):
    return ext("SKIP", "Q1.D02", *[val(k, None, "REJECTED", "unspecified") for k in keys], reason=reason, targets=list(keys))


def unclear(key, reason="OUT_OF_RANGE"):
    return ext("REASK", "Q1.D01", val(key, None, "UNCLEAR", "unspecified"), reason=reason, targets=[key])


def test_unknown_from_hold_is_recorded_with_item_and_slot(env):
    env.start("K1")
    env.say("K1", "모르겠어요", "t1", hold("Q1.D01.diagnosed", "Q1.D01.on_medication"))
    r = result(env)
    assert "skipped" not in r                                                           # 문진 스키마 2.0.0: 문자열 목록 skipped는 폐지됐다
    assert r["skipped_details"] == [{"item": "Q1.D01", "slot": "Q1.D01.diagnosed", "reason": "UNKNOWN"},
                                    {"item": "Q1.D01", "slot": "Q1.D01.on_medication", "reason": "UNKNOWN"}]


def test_rejected_from_skip_refused_and_request_skip(env):
    env.start("K1")
    env.say("K1", "말하기 싫어요", "t1", skip("REFUSED", "Q1.D01.diagnosed", "Q1.D01.on_medication"))
    r = result(env)
    assert {(d["slot"], d["reason"]) for d in r["skipped_details"]} == {("Q1.D01.diagnosed", "REJECTED"), ("Q1.D01.on_medication", "REJECTED")}
    env2 = env
    env2.start("K2")
    env2.say("K2", "다음에 할게요", "t1", skip("REQUEST_SKIP", "Q1.D01.diagnosed"))
    assert [(d["item"], d["reason"]) for d in result(env2, "K2")["skipped_details"]] == [("Q1.D01", "REJECTED")]


def test_unclear_that_never_got_a_value_is_recorded_and_a_later_answer_removes_it(env):
    env.start("K1")
    env.say("K1", "여덟 일이요", "t1", unclear("Q1.D01.diagnosed"))
    assert result(env)["skipped_details"] == [{"item": "Q1.D01", "slot": "Q1.D01.diagnosed", "reason": "UNCLEAR"}]
    env.say("K1", "아니요", "t2", ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", False), val("Q1.D01.on_medication", False)))
    assert result(env)["skipped_details"] == []                                         # 나중에 값이 저장되면 빈칸이 아니다


def test_the_latest_cause_wins_and_a_missing_slot_is_not_a_blank(env):
    env.start("K1")
    env.say("K1", "음", "t1", unclear("Q1.D01.diagnosed", "VAGUE_QUANTITY"))
    env.say("K1", "모르겠어요", "t2", hold("Q1.D01.diagnosed"))
    assert [(d["slot"], d["reason"]) for d in result(env)["skipped_details"]] == [("Q1.D01.diagnosed", "UNKNOWN")]
    env.start("K3")
    env.say("K3", "음", "t1", ext("REASK", "Q1.D01", val("Q1.D01.on_medication", None, "MISSING", "unspecified"), reason="MISSING", targets=["Q1.D01.on_medication"]))
    assert result(env, "K3")["skipped_details"] == []                                   # MISSING은 아직 못 받은 것이지 원인이 정해진 빈칸이 아니다


def test_details_hold_no_utterance_text_and_only_the_documented_keys(env):
    env.start("K1")
    env.say("K1", "비밀발화내용 모르겠어요", "t1", hold("Q1.D01.diagnosed"))
    r = result(env)
    assert "비밀발화내용" not in json.dumps(r, ensure_ascii=False)
    assert all(set(d) <= {"item", "slot", "reason"} and d["reason"] in ("UNKNOWN", "REJECTED", "UNCLEAR") for d in r["skipped_details"])


def test_a_skipped_item_without_slot_information_is_still_explained(env):
    """옛 세션처럼 대화 로그에서 칸을 찾을 수 없는 건너뛴 문항은 문항만 적고 칸은 생략한다."""
    from survey_service.blanks import blank_details
    doc = {"progress_record": {"skipped": [{"question_id": "Q2.D01", "reason": "UNCERTAIN"}, {"question_id": "Q2.D02", "reason": "REFUSED"},
                                           {"question_id": "Q2.D03", "reason": "REQUEST_SKIP"}]}, "dialogue_log": [], "answer_records": []}
    assert blank_details(doc) == [{"item": "Q2.D01", "reason": "UNKNOWN"}, {"item": "Q2.D02", "reason": "REJECTED"}, {"item": "Q2.D03", "reason": "REJECTED"}]


def test_session_result_schema_2_0_0_has_no_skipped_field_and_requires_skipped_details():
    import survey_schema
    d = survey_schema.load_schema("SessionResult")["$defs"]["SessionResult"]
    assert "skipped" not in d["properties"] and "skipped" not in d["required"] and "skipped_details" in d["required"]
