"""U48: 위험 규칙 파일 형식은 계약 SurveyRiskRulesFile(boundary-contracts 2.8.0)이 정본이다. parse_rules는 형식 검사를 계약 validator에 맡기고
스키마로 못 하는 것(규칙 id 중복, 정규식 유효성)만 따로 거부한다 (ADR-0042). 거부 메시지와 동작은 바뀌지 않는다:
기대 메시지(tests/fixtures_risk_rules_messages.json)는 손으로 검사하던 이전 구현의 실제 출력을 그대로 적은 것이다."""
import copy
import json
from pathlib import Path

import pytest
from boundary_contracts import validator

from survey_service.components.risk_screen import parse_rules
from survey_service.settings import ConfigError

GOOD = {"version": "v1", "rules": [{"id": "r1", "description": "d", "match": {"any_of": [{"contains": "abc"}, {"regex": "a+b"}]}}]}


def _mod(f):
    d = copy.deepcopy(GOOD)
    f(d)
    return d


CASES = {
    "not_object": [1, 2],
    "extra_top": _mod(lambda d: d.update(zzz=1)),
    "no_version": _mod(lambda d: d.pop("version")),
    "empty_version": _mod(lambda d: d.update(version="  ")),
    "version_int": _mod(lambda d: d.update(version=3)),
    "no_rules": _mod(lambda d: d.pop("rules")),
    "empty_rules": _mod(lambda d: d.update(rules=[])),
    "rules_not_list": _mod(lambda d: d.update(rules={})),
    "rule_not_object": _mod(lambda d: d.update(rules=["x"])),
    "rule_extra": _mod(lambda d: d["rules"][0].update(zz=1)),
    "rule_no_id": _mod(lambda d: d["rules"][0].pop("id")),
    "rule_bad_id": _mod(lambda d: d["rules"][0].update(id="-bad")),
    "rule_long_id": _mod(lambda d: d["rules"][0].update(id="a" * 65)),
    "dup_id": _mod(lambda d: d["rules"].append(copy.deepcopy(d["rules"][0]))),
    "no_desc": _mod(lambda d: d["rules"][0].pop("description")),
    "blank_desc": _mod(lambda d: d["rules"][0].update(description=" ")),
    "no_match": _mod(lambda d: d["rules"][0].pop("match")),
    "match_not_obj": _mod(lambda d: d["rules"][0].update(match=[])),
    "match_extra": _mod(lambda d: d["rules"][0]["match"].update(zz=1)),
    "no_any_of": _mod(lambda d: d["rules"][0]["match"].pop("any_of")),
    "empty_any_of": _mod(lambda d: d["rules"][0]["match"].update(any_of=[])),
    "cond_two_keys": _mod(lambda d: d["rules"][0]["match"].update(any_of=[{"contains": "a", "regex": "b"}])),
    "cond_unknown_key": _mod(lambda d: d["rules"][0]["match"].update(any_of=[{"equals": "a"}])),
    "cond_not_obj": _mod(lambda d: d["rules"][0]["match"].update(any_of=["a"])),
    "contains_blank": _mod(lambda d: d["rules"][0]["match"].update(any_of=[{"contains": " "}])),
    "contains_int": _mod(lambda d: d["rules"][0]["match"].update(any_of=[{"contains": 1}])),
    "regex_long": _mod(lambda d: d["rules"][0]["match"].update(any_of=[{"regex": "a" * 501}])),
    "regex_invalid": _mod(lambda d: d["rules"][0]["match"].update(any_of=[{"regex": "("}])),
    "second_rule_bad": _mod(lambda d: d["rules"].append({"id": "r2", "description": "d", "match": {"any_of": [{"contains": ""}]}})),
}
EXPECTED = json.loads((Path(__file__).parent / "fixtures_risk_rules_messages.json").read_text(encoding="utf-8"))


def test_the_case_table_and_the_recorded_messages_cover_the_same_names():
    assert set(CASES) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(CASES))
def test_rejection_message_is_unchanged(name):
    with pytest.raises(ConfigError) as e:
        parse_rules(json.dumps(CASES[name]).encode())
    assert str(e.value) == EXPECTED[name]


def test_the_good_document_passes_the_contract_and_parses():
    assert list(validator("SurveyRiskRulesFile").iter_errors(GOOD)) == []
    version, rules = parse_rules(json.dumps(GOOD).encode())
    assert version == "v1" and [r.id for r in rules] == ["r1"] and len(rules[0].regex) == 1


def test_the_demo_rules_file_passes_the_contract_and_parses():
    raw = (Path(__file__).resolve().parents[1] / "examples/risk-rules.demo.json").read_bytes()
    assert list(validator("SurveyRiskRulesFile").iter_errors(json.loads(raw))) == []
    parse_rules(raw)


def test_what_the_schema_cannot_say_is_still_rejected_by_the_service():
    """계약 스키마는 id 중복과 정규식 유효성을 표현하지 못한다(스키마 설명이 서비스가 따로 거부한다고 적는다)."""
    for name in ("dup_id", "regex_invalid"):
        assert list(validator("SurveyRiskRulesFile").iter_errors(CASES[name])) == []
        with pytest.raises(ConfigError):
            parse_rules(json.dumps(CASES[name]).encode())
