"""D74 순수 로직(survey_service/extraction.py, flow.py, response_tables.py)을 학습 데이터셋 v2 표본으로 확인한다.

tests/fixtures_dataset_v2.json은 학습 서버 val 파일에서 읽은 소량 표본(A 입력·A 출력·B 입력 16건, 환자 말은 합성 데이터)을 인용한 것이다.
데이터 전체를 가져오지 않는다. 전체 일치율은 `scripts/table_agreement.py`로 학습 서버 파일을 표준출력으로 흘려 이 PC에서 계산한다(docs/design.md 16절)."""
import copy
import json
from pathlib import Path

import pytest
from boundary_contracts import validator
from boundary_contracts.action_rules import check_action_extraction

import survey_schema

from survey_service import extraction
from survey_service.assembly import Bank
from survey_service.flow import build_flow, load_flow
from survey_service.response_tables import ResponseTables, josa
from survey_service.store import effects

from .sample_bank import sample_bank

FIX = json.loads((Path(__file__).parent / "fixtures_dataset_v2.json").read_text(encoding="utf-8"))
FLOW = load_flow()
TABLES = ResponseTables(survey_schema.load_data("response_tables"))
BANK = Bank(sample_bank())


# ---------------------------------------------------------------- 문항 순서·건너뛰기 표 (D76)
def test_flow_built_from_survey_schema_is_the_contract_flow_table():
    data = survey_schema.load_data("question_flow")
    assert FLOW.table.wire() == {"order": data["order"], "skips": data["skips"]}
    assert list(validator("QuestionFlowTable").iter_errors(FLOW.table.wire())) == []
    assert FLOW.table.order[0] == "Q1.D01" and len(FLOW.table.order) == 31 and len(FLOW.table.skips) == 7


def test_flow_data_validates_against_its_contract_and_has_numeric_condition():
    data = survey_schema.load_data("question_flow")
    assert {"slot": "Q8_1.days", "op": "==", "value": 0, "skip_item": "Q8_2"} in data["skips"]
    assert FLOW.table.is_skipped("Q8_2", {"Q8_1.days": 0}) and not FLOW.table.is_skipped("Q8_2", {"Q8_1.days": 1})
    assert not FLOW.table.is_skipped("Q4_1", {"Q4.answer": 0})                      # bool과 수는 다르다
    bad = copy.deepcopy(data); bad["skips"][0]["op"] = "<="
    import jsonschema
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, survey_schema.load_schema("QuestionFlow"))


def test_flow_follows_skips_for_next_item():
    assert FLOW.previous_item("Q8_1", {"Q7.does_not_drink": True}) == "Q7"
    assert FLOW.previous_item("Q8_1", {"Q7.does_not_drink": False}) == "Q7_2"
    assert FLOW.previous_item("Q1.D01", {}) is None


def test_tables_data_validate_against_contract_and_state_the_source_and_unconfirmed_status():
    import jsonschema
    data = survey_schema.load_data("response_tables")
    jsonschema.validate(data, survey_schema.load_schema("ResponseTables"))
    assert data["table_status"] == "DATASET_RULES_UNCONFIRMED" and "확인 대기" in data["note"] and "labels.py" in data["note"] and "2026-10-14" in data["note"]
    assert data["source"]["file"].endswith("labels.py") and data["source"]["approval"] == "승인 기록 미확인"
    assert "학습 분포 밖" in data["note"]                                     # 원본에 없는 서비스 쪽 판단(remaining)을 숨기지 않는다
    for item in FLOW.table.order:                                            # 모든 문항에 topic과 짧은 이름이 있다
        assert TABLES.topic(item) and TABLES.short_topic(item)


# ---------------------------------------------------------------- A 입력
def doc_for(a_input: dict) -> dict:
    """A 입력의 state가 나오는 세션 문서를 거꾸로 만든다."""
    doc = effects.initial_state("S", sample_bank(), a_input["question_id"])
    ups, opens = [], []
    for k, v in a_input["state"].items():
        if v["status"] == "ANSWERED":
            ups.append({"slot": effects.slot_ref(k), "action": "FIRST_WRITE", "value": v["value"], "precision": "exact"})
        else:
            opens.append({"key": k, "status": v["status"]})
    doc["answer_records"] = effects.apply_answer_updates(doc["answer_records"], ups)
    doc["progress_record"]["open_slots"] = opens
    doc["dialogue_log"].append({"response_text": a_input["current_question"]})
    return doc


@pytest.mark.parametrize("fx", FIX, ids=[f"{f['a_output']['action']}-{i}" for i, f in enumerate(FIX)])
def test_extract_input_equals_training_data_character_for_character(fx):
    ai = fx["a_input"]
    got = extraction.build_extract_input(doc_for(ai), BANK, FLOW, ai["user_utterance"])
    assert json.dumps(got, ensure_ascii=False) == json.dumps(ai, ensure_ascii=False)      # 최상위 키 알파벳 순, state는 삽입 순서


def test_state_order_is_current_saved_then_previous_and_reference_then_unsaved():
    doc = effects.initial_state("S", sample_bank(), "Q9_2")
    def put(k, v):
        doc["answer_records"] = effects.apply_answer_updates(doc["answer_records"], [{"slot": effects.slot_ref(k), "action": "FIRST_WRITE", "value": v, "precision": "exact"}])
    for k, v in [("Q8_1.days", 3), ("Q8_2.duration_minutes", 30), ("Q9_1.days", 2)]:
        put(k, v)
    doc["progress_record"]["open_slots"] = [{"key": "Q9_2.duration_minutes", "status": "UNCLEAR"}]
    st = extraction.build_state(doc, FLOW)
    # Q9_2: 직전 문항 Q9_1, 참조 원천 Q8_2, 그 뒤 지금 문항의 미저장 칸
    assert list(st) == ["Q9_1.days", "Q8_2.duration_minutes", "Q9_2.duration_minutes"]
    assert list(st["Q9_1.days"]) == ["value", "status"] and st["Q9_2.duration_minutes"] == {"value": None, "status": "UNCLEAR"}


def test_state_slot_order_inside_a_question_follows_the_survey_schema():
    doc = effects.initial_state("S", sample_bank(), "Q5")
    ups = [{"slot": effects.slot_ref(f"Q4_1.{s}"), "action": "FIRST_WRITE", "value": v, "precision": "exact"}
           for s, v in [("daily_count", 5), ("total_years", 20), ("status", "former"), ("years_since_quit", 2)]]
    doc["answer_records"] = effects.apply_answer_updates(doc["answer_records"], ups)
    assert list(extraction.build_state(doc, FLOW)) == ["Q4_1.status", "Q4_1.total_years", "Q4_1.daily_count", "Q4_1.years_since_quit"]


def test_previous_question_is_skipped_over_when_its_skip_rule_holds():
    doc = effects.initial_state("S", sample_bank(), "Q8_1")
    doc["answer_records"] = effects.apply_answer_updates(doc["answer_records"], [
        {"slot": effects.slot_ref("Q7.does_not_drink"), "action": "FIRST_WRITE", "value": True, "precision": "exact"}])
    assert list(extraction.build_state(doc, FLOW)) == ["Q7.does_not_drink"]


def test_extract_input_uses_question_sentence_when_there_is_no_previous_response():
    doc = effects.initial_state("S", sample_bank(), "Q1.D01")
    got = extraction.build_extract_input(doc, BANK, FLOW, "네")
    assert got["current_question"] == BANK.sentence("Q1.D01") and list(got) == ["current_question", "question_id", "state", "user_utterance"]
    assert validator("ActionExtractionInput").is_valid(got)


# ---------------------------------------------------------------- 검증과 저장 계획
@pytest.mark.parametrize("fx", FIX, ids=[f"{f['a_output']['action']}-{i}" for i, f in enumerate(FIX)])
def test_sample_outputs_pass_the_contract_checker_with_the_schema_flow(fx):
    assert check_action_extraction(fx["a_input"], fx["a_output"], FLOW.table.wire()) == []


@pytest.mark.parametrize("fx", FIX, ids=[f"{f['a_output']['action']}-{i}" for i, f in enumerate(FIX)])
def test_plan_writes_saves_only_save_true_values(fx):
    doc = doc_for(fx["a_input"])
    out = fx["a_output"]
    w = extraction.plan_writes(doc, out)
    saved_keys = {v["key"] for v in out["values"] if v["save"]}
    unsaved_keys = {v["key"] for v in out["values"] if not v["save"]}
    written = {effects.model_key(u["slot"]) for u in w["answer_updates"]}
    assert written <= saved_keys and not written & unsaved_keys             # save=false(CONFLICT 등)는 저장하지 않는다
    assert w["progress_record_update"]["current_question_id"] == out["next_item"]
    after = effects.apply_writes(doc, w)                                                  # 적용할 수 있는 쓰기다
    assert after["progress_record"]["current_question_id"] == out["next_item"]


# ---------------------------------------------------------------- B 입력 (표 찾기)
@pytest.mark.parametrize("fx", FIX, ids=[f"{f['a_output']['action']}-{i}" for i, f in enumerate(FIX)])
def test_response_request_matches_training_data_fields(fx):
    ai, out, bi = fx["a_input"], fx["a_output"], fx["b_input"]
    doc = doc_for(ai)
    after = effects.apply_writes(doc, extraction.plan_writes(doc, out))
    got = extraction.build_response_request(out, ai, after, BANK, TABLES)
    assert validator("ResponseRequest").is_valid(got)
    for k in ("action", "reason", "topic", "user_utterance", "known", "ask", "previous", "current", "hold", "note"):
        assert got.get(k) == bi.get(k), k
    assert got["next"]["type"] == bi["next"]["type"] and got["next"].get("question_id") == bi["next"].get("question_id")
    def keys_sorted(x):
        return not isinstance(x, dict) or (list(x) == sorted(x) and all(keys_sorted(v) for v in x.values()))
    assert keys_sorted(got) and json.dumps(got, ensure_ascii=False).count("  ") == 0


def test_next_text_is_the_question_bank_sentence_not_the_training_paraphrase():
    out = {"action": "NEXT", "next_item": "Q1.D03", "values": [
        {"key": "Q1.D02.diagnosed", "value": False, "precision": "exact", "status": "ANSWERED", "save": True}]}
    ai = {"current_question": "x", "question_id": "Q1.D02", "state": {}, "user_utterance": "아니요"}
    doc = effects.initial_state("S", sample_bank(), "Q1.D02")
    got = extraction.build_response_request(out, ai, doc, BANK, TABLES)
    assert got["next"] == {"question_id": "Q1.D03", "text": BANK.sentence("Q1.D03"), "type": "NEXT"}


def test_partial_prefilled_next_text_asks_only_remaining_slots():
    """57행 문제: 데이터는 이미 저장된 Q4_1.status를 다시 묻는 문장을 next.text에 둔다. 서비스는 남은 칸만 묻는다."""
    out = {"action": "NEXT", "next_item": "Q4_1", "values": [
        {"key": "Q4.answer", "value": True, "precision": "exact", "status": "ANSWERED", "save": True},
        {"key": "Q4_1.status", "value": "former", "precision": "exact", "status": "ANSWERED", "save": True}]}
    ai = {"current_question": "x", "question_id": "Q4", "state": {}, "user_utterance": "예전에 피웠고 끊었어요"}
    doc = effects.initial_state("S", sample_bank(), "Q4")
    after = effects.apply_writes(doc, extraction.plan_writes(doc, out))
    got = extraction.build_response_request(out, ai, after, BANK, TABLES)
    assert got["next"]["text"] == "일반담배는 피운 기간, 하루 개비 수, 끊은 지 얼마나 됐는지 말씀해 주세요."
    assert "지금 피우는지" not in got["next"]["text"]                         # 이미 정해진 칸은 묻지 않는다
    # 현재 흡연자라면 끊은 지 얼마나 됐는지는 묻지 않는다(only_if)
    assert TABLES.remaining_text("Q4_1", {"Q4_1.status": "current"}) == "일반담배는 피운 기간, 하루 개비 수를 말씀해 주세요."
    # 남은 칸을 알 수 없으면(표에 칸 설명이 없는 문항, 남은 칸 없음) 문항 은행 문장 그대로다
    assert TABLES.remaining_text("Q1.D01", {"Q1.D01.diagnosed": True}) is None
    assert TABLES.remaining_text("Q4_1", {"Q4_1.status": "current", "Q4_1.total_years": 1, "Q4_1.daily_count": 1}) is None


def test_start_request_is_valid_and_has_no_user_fields():
    req = extraction.build_start_request(BANK, TABLES, "Q1.D01")
    assert validator("ResponseRequest").is_valid(req) and set(req) == {"action", "next", "topic"}
    assert req["next"] == {"question_id": "Q1.D01", "text": BANK.sentence("Q1.D01"), "type": "NEXT"}


def test_ask_uses_the_datasets_rule_and_only_falls_back_when_that_rule_cannot_produce_a_sentence():
    n = TABLES.stats["ask_fallback"]
    # 예전 초안은 이 조합에 규칙이 없어 폴백이었다. 원본 규칙은 문장을 만든다
    assert TABLES.ask_or_topic("MISSING", ["Q3.answer"], {}, "Q3") == "answer"          # 원본 _ask_missing의 마지막 분기(칸 이름이 없으면 칸 코드)
    assert TABLES.stats["ask_fallback"] == n
    # 원본이 예외를 던질 입력(표에 없는 문항의 진단 칸)만 폴백하고 센다
    assert TABLES.ask_or_topic("MISSING", ["Q1.D99.diagnosed"], {}, "Q3") == TABLES.topic("Q3")
    assert TABLES.stats["ask_fallback"] == n + 1


@pytest.mark.parametrize("word,pair,want", [("와인", "을/를", "와인을"), ("소주", "을/를", "소주를"), ("양주(위스키 등)", "을/를", "양주(위스키 등)를"),
                                             ("4", "이/가", "4가"), ("3", "이/가", "3이"), ("1.5", "이/가", "1.5가")])
def test_josa(word, pair, want):
    assert josa(word, pair) == want


def test_values_of_each_kind_become_phrases():
    def p(key, value, prec="exact"):
        out = {"values": [{"key": key, "value": value, "precision": prec, "status": "ANSWERED", "save": True}], "action": "NEXT", "next_item": "END"}
        return TABLES.b_fields(out, key.split(".")[0].split("[")[0], "발화", {})["known"]
    assert p("Q10.days", 3, "approximate") == "최근 일주일 3일쯤" and p("Q9_2.duration_minutes", 135) == "하루 2시간 15분"
    assert p("Q5_1.years_since_quit", 0.5) == "끊은 지 반년" and p("Q6_1.use_pattern", "daily") == "최근 한 달 매일 씀"
    assert p("Q7_1[spirits].amount", 7) == "양주(위스키 등) 7 (단위는 아직 모름)"
    both = {"values": [{"key": "Q7_1[beer].amount", "value": 1, "precision": "exact", "status": "ANSWERED", "save": True},
                       {"key": "Q7_1[beer].unit", "value": "can", "precision": "exact", "status": "ANSWERED", "save": True}], "action": "NEXT", "next_item": "Q7_2"}
    assert TABLES.b_fields(both, "Q7_1", "발화", {})["known"] == "맥주 1캔"
