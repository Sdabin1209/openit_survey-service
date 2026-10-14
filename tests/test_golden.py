"""골든 예시(tests/golden)와 구현 출력의 대조. 방법과 한계는 golden_replay.py와 docs/design.md 10절, CR-002.

차이는 (a) 구현 결함 (b) 규칙 해석 차이 (c) 예시 오류로 분류해 golden_known.json에 근거와 함께 둔다.
(a)는 고쳐서 남지 않아야 한다. 새로 생긴 차이와 사라진 차이 모두 실패다.
"""
import json
from pathlib import Path

import jsonschema
import pytest

import survey_schema

from .golden_replay import GOLDEN, compare_all, load_cases

KNOWN = json.loads((Path(__file__).parent / "golden_known.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def result():
    return compare_all()


def test_cases_loaded():
    cases = load_cases()
    assert len(cases) == 226


def test_every_specimen_matches_its_contract_schema():
    validators, bad = {}, []
    n = 0
    for f in sorted(GOLDEN.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for s in d["specimens"]:
            v = validators.setdefault(s["contract"], jsonschema.Draft202012Validator(survey_schema.load_schema(s["contract"])))
            n += 1
            errs = list(v.iter_errors(s["data"]))
            if errs:
                bad.append((d["case"], s["id"], errs[0].message[:100]))
    assert n > 1000 and not bad, bad[:5]


def test_known_differences_are_classified_with_reason():
    for key, (cat, reason) in KNOWN.items():
        assert cat in ("a", "b", "c"), key
        assert reason.strip(), key
    assert not [k for k, (c, _) in KNOWN.items() if c == "a"], "(a) 구현 결함은 남기지 않고 고친다"


def test_golden_matches_except_classified(result):
    total, diffs = result
    assert total > 1000
    unexpected = {k: v[:2] for k, v in diffs.items() if k not in KNOWN}
    stale = [k for k in KNOWN if k not in diffs]
    assert not unexpected, f"분류되지 않은 차이 {len(unexpected)}건: {list(unexpected.items())[:5]}"
    assert not stale, f"더는 다르지 않은 분류 항목: {stale}"
