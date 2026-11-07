"""Public identifiers cannot gain a trailing newline through regex engine differences."""
import copy
import json
from pathlib import Path

import jsonschema
import pytest
import survey_schema


@pytest.mark.parametrize("value", ["sid", "sid-1", "SID_2"])
def test_frontend_session_id_rejects_final_newline(value):
    schema = survey_schema.load_schema("FrontendInput")
    validate = jsonschema.Draft202012Validator(schema)
    record = {"response_text": "응답", "turn_failure_signal": None, "session_id": value}
    assert validate.is_valid(record)
    record["session_id"] += "\n"
    assert not validate.is_valid(record)


def test_old_dollar_pattern_would_accept_the_rejected_record():
    schema = copy.deepcopy(survey_schema.load_schema("FrontendInput"))
    node = schema["$defs"]["FrontendInput"]["properties"]["session_id"]
    node["pattern"] = node["pattern"].replace("$(?![\\s\\S])", "$")
    record = {"response_text": "응답", "turn_failure_signal": None, "session_id": "sid\n"}
    assert jsonschema.Draft202012Validator(schema).is_valid(record)
