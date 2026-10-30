"""문진 스키마마다 통과 예시(<이름>.valid.json)와 거부 예시(<이름>.invalid.json)가 있고 결과가 맞는지 확인한다.

예시는 schema/schemas/examples/에 있다. 통과 예시는 시험이 만드는 실제 값(세션 문서, Trace, 문항 은행, 데이터 표)이고
거부 예시는 필수·열거·형식 중 하나를 의도적으로 어긴 것이다. examples/는 wheel에 들어가지 않는다(setup.py는 schemas/*.json만 복사)."""
import json
from pathlib import Path

import jsonschema
import pytest

import survey_schema

SCHEMAS = Path(__file__).resolve().parents[1] / "schema" / "schemas"
EXAMPLES = SCHEMAS / "examples"
NAMES = sorted(p.stem for p in SCHEMAS.glob("*.json"))


def _validator(name):
    return jsonschema.Draft202012Validator(survey_schema.load_schema(name))


def test_there_are_schemas():
    assert len(NAMES) == 16


@pytest.mark.parametrize("name", NAMES)
def test_valid_example_passes(name):
    f = EXAMPLES / f"{name}.valid.json"
    assert f.is_file(), f"{name}: 통과 예시가 없다"
    errs = [e.message for e in _validator(name).iter_errors(json.loads(f.read_text(encoding="utf-8")))]
    assert errs == []


@pytest.mark.parametrize("name", NAMES)
def test_invalid_example_is_rejected(name):
    f = EXAMPLES / f"{name}.invalid.json"
    assert f.is_file(), f"{name}: 거부 예시가 없다"
    assert list(_validator(name).iter_errors(json.loads(f.read_text(encoding="utf-8")))), f"{name}: 거부 예시가 통과했다"


def test_no_stray_example_files():
    expected = {f"{n}.{k}.json" for n in NAMES for k in ("valid", "invalid")}
    assert {p.name for p in EXAMPLES.glob("*.json")} == expected
