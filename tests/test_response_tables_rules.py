"""표 찾기가 데이터셋 담당의 규칙(학습 서버 ~/WS/dataset/scripts/labels.py, 2026-10-14)과 글자 단위로 같은 문장을 내는지.

`tests/data/response_tables_golden.json`은 그 규칙 함수가 만든 입력-출력 쌍이다(출력 문장마다 하나씩만 고른 대표 쌍, 학습 데이터 원문이 아니다).
원본 코드와 학습 데이터 전수 대조는 서버 데이터가 필요해 이 시험에 없다(결과는 docs/STATUS.md와 CHANGELOG에 쓴다).
"""
import json
from pathlib import Path

import pytest
import survey_schema

from survey_service.response_tables import ResponseTables, josa, parse_key


GOLD = json.loads((Path(__file__).parent / "data" / "response_tables_golden.json").read_text(encoding="utf-8"))
T = ResponseTables(survey_schema.load_data("response_tables"))


def test_golden_is_small_and_not_server_data():
    assert "학습 데이터 원문이 아니라" in GOLD["_comment"] or "규칙 함수의 출력" in GOLD["_comment"]
    assert len(GOLD["ask"]) > 100 and len(GOLD["value"]) > 100 and len(GOLD["b"]) > 100


@pytest.mark.parametrize("row", GOLD["ask"], ids=lambda r: f"{r[0]}-{r[1][0]}")
def test_ask_text_matches_the_datasets_rule(row):
    reason, targets, rows, want = row
    assert T.ask_text(reason, targets, {(i, rk, f): v for i, rk, f, v in rows}) == want


@pytest.mark.parametrize("row", GOLD["value"], ids=lambda r: f"{r[0]}.{r[1]}={r[2]}/{r[4]}")
def test_value_text_matches_the_datasets_rule(row):
    item, field, value, rk, prec, want = row
    assert T.value_text(item, field, value, rk, prec) == want


@pytest.mark.parametrize("i", range(len(GOLD["b"])))
def test_b_fields_match_the_datasets_b_input(i):
    row = GOLD["b"][i]
    got = T.b_fields(row["a_out"], row["asked_item"], row["utterance"], row["state"])
    assert got == row["expected"]
    assert list(got) == list(got) and json.dumps(got, ensure_ascii=False, sort_keys=True) == json.dumps(row["expected"], ensure_ascii=False, sort_keys=True)


def test_original_fallback_cases_are_counted_not_invented():
    t = ResponseTables(survey_schema.load_data("response_tables"))
    # 원본 규칙이 예외를 던지는 입력: 표에 없는 문항(진단 칸), 표에 없는 Q3 값 -> 지어내지 않고 폴백 문장 + 카운터
    out = {"values": [], "action": "REASK", "reason": "MISSING", "targets": ["Q1.D99.diagnosed"], "next_item": "Q1.D99"}
    b = t.b_fields(out, "Q1.D99", "발화", {})
    assert b["topic"] == "Q1.D99" and b["ask"] == "Q1.D99" and t.stats["ask_fallback"] == 1 and t.stats["phrase_fallback"] >= 1
    out = {"values": [{"key": "Q3.answer", "value": "maybe", "precision": "exact", "status": "ANSWERED", "save": True}], "action": "NEXT", "next_item": "Q4"}
    assert t.b_fields(out, "Q3", "발화", {})["known"] == "answer: maybe"          # 원본의 마지막 분기 문형(칸: 값)으로 대신하고 센다
    assert t.stats["phrase_fallback"] >= 2


def test_original_generic_branches_are_the_datasets_own_sentences_not_fallbacks():
    t = ResponseTables(survey_schema.load_data("response_tables"))
    # 학습 데이터에 실제로 나오는 조합: 사유 UNPARSED_VALUE(원본 ask_text의 일반 분기) x Q10.days
    out = {"values": [], "action": "REASK", "reason": "UNPARSED_VALUE", "targets": ["Q10.days"], "next_item": "Q10"}
    assert t.b_fields(out, "Q10", "발화", {})["ask"] == "날 수를 다시"
    assert t.stats["ask_fallback"] == 0 and t.stats["ask_generic"] == 1


def test_josa_follows_the_datasets_rule():
    assert [josa(w, "을/를") for w in ("소주", "와인", "양주(위스키 등)", "1565", "3")] == ["소주를", "와인을", "양주(위스키 등)를", "1565를", "3을"]
    assert josa("2", "이/가") == "2가" and josa("6", "이/가") == "6이"      # 숫자 끝소리 대략: 0·1·3·6·7·8은 받침 있음


def test_remaining_text_is_the_only_service_side_judgement_and_is_marked_outside_the_training_distribution():
    assert T.remaining_text("Q4_1", {"Q4_1.status": "current"}) == "일반담배는 피운 기간, 하루 개비 수를 말씀해 주세요."
    assert "학습 분포 밖" in T.data["note"] and set(T.data["remaining"]) == {"Q4_1", "Q5_1"}
    assert parse_key("Q7_1[soju].amount") == ("Q7_1", "soju", "amount")
