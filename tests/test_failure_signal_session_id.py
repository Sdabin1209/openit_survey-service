"""Failure signals use the public session identifier format in every embedded copy."""
from boundary_contracts import schema_validator
import json
from pathlib import Path

import jsonschema
import pytest

SCHEMAS = Path(__file__).resolve().parents[1] / "schema" / "schemas"
COPIES = [p for p in sorted(SCHEMAS.glob("*.json"))
          if "TurnFailureSignal" in json.loads(p.read_text())["$defs"]]


@pytest.mark.parametrize("path", COPIES, ids=lambda p: p.stem)
@pytest.mark.parametrize("value,valid", [
    ("sid", True), ("SID_2-3", True), ("a" * 64, True),
    ("", False), ("a" * 65, False), ("sid\n", False),
    ("sid/path", False), ("세션", False),
])
def test_failure_signal_session_id_format(path, value, valid):
    doc = json.loads(path.read_text())
    schema = {**doc, "$ref": "#/$defs/TurnFailureSignal/properties/session_id"}
    assert schema_validator(schema).is_valid(value) is valid


def test_all_failure_signal_copies_are_covered():
    assert {p.stem for p in COPIES} == {
        "FrontendInput", "OrchestratorOutput", "SessionStore", "StaffConsoleInput", "TurnTrace",
    }
