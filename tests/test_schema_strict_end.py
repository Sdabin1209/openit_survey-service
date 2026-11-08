"""Public identifiers cannot gain a trailing newline through regex engine differences."""
from boundary_contracts import schema_validator
import copy
import json
from pathlib import Path

import jsonschema
import pytest
import survey_schema


@pytest.mark.parametrize("value", ["sid", "sid-1", "SID_2"])
def test_frontend_session_id_rejects_final_newline(value):
    schema = survey_schema.load_schema("FrontendInput")
    validate = schema_validator(schema)
    record = {"response_text": "응답", "turn_failure_signal": None, "session_id": value}
    assert validate.is_valid(record)
    record["session_id"] += "\n"
    assert not validate.is_valid(record)


def test_the_contract_session_id_pattern_is_the_one_that_rejects_the_final_newline():
    """패턴은 이제 계약 _common.SessionId 의 것이다(문진 스키마는 $ref 로 위임). 끝 줄바꿈 거부가 그 정의에 있다는 것을 직접 본다."""
    from boundary_contracts import load_schema
    assert load_schema("_common")["$defs"]["SessionId"]["pattern"].endswith("(?![\\s\\S])")
