"""U32: 문항 은행 칸 정의(SlotDef)에서 계약 QuestionFieldSpec을 만들어 I9 extract 요청 fields로 싣는다 (ADR-0033, 계약 2.4.0).
기대값 tests/fixtures_question_fields.json은 학습 데이터셋 question_fields·field_options(docs/SQL.md 5.2~5.3)의 52칸(boolean 31)이다."""
import copy
import json
from types import SimpleNamespace
from pathlib import Path

import jsonschema
import pytest
import survey_schema
from boundary_contracts import validator

from survey_service.fields import build_field_specs
from survey_service.remote import RemoteModelSession

from .e2e_support import Rig, ext, val
from .sample_bank import sample_bank

EXPECTED = json.loads((Path(__file__).parent / "fixtures_question_fields.json").read_text(encoding="utf-8"))


def test_the_bank_with_typed_slots_still_validates_against_the_survey_schema():
    jsonschema.Draft202012Validator(survey_schema.load_schema("QuestionBank")).validate(sample_bank())


def test_slot_type_properties_are_checked_by_the_schema():
    bad = sample_bank()
    bad["questions"][0]["slots"][0]["data_type"] = "text"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(survey_schema.load_schema("QuestionBank")).validate(bad)


def test_field_specs_from_the_bank_equal_the_dataset_table_and_pass_the_contract_schema():
    specs = build_field_specs(sample_bank())
    assert list(validator("QuestionFieldSpec").iter_errors(specs)) == []
    assert len(specs["fields"]) == 52 and sum(1 for f in specs["fields"] if f["data_type"] == "boolean") == 31
    assert {f["slot"]: f for f in specs["fields"]} == {f["slot"]: f for f in EXPECTED["fields"]}


def test_key_values_match_the_dataset():
    by = {f["slot"]: f for f in build_field_specs(sample_bank())["fields"]}
    assert by["Q7.frequency"] == {"slot": "Q7.frequency", "data_type": "number", "exclusive_minimum": 0}
    assert by["Q10.days"] == {"slot": "Q10.days", "data_type": "integer", "unit": "day/week", "minimum": 0, "maximum": 7}
    assert by["Q6_1.use_pattern"]["options"] == ["daily", "days_10_29", "days_1_2", "days_3_9", "no"]
    assert by["Q7_1.amount"]["repeat_keys"] == ["soju", "beer", "spirits", "makgeolli", "wine"] and by["Q7_1.amount"]["minimum"] == 0
    assert "unit" not in by["Q7_1.amount"] and by["Q4_1.daily_count"]["unit"] == "개비"


def test_a_bank_without_typed_slots_has_no_field_specs():
    bank = copy.deepcopy(sample_bank())
    for q in bank["questions"]:
        for s in q["slots"]:
            for k in ("data_type", "unit", "minimum", "maximum", "exclusive_minimum"):
                s.pop(k, None)
    assert build_field_specs(bank) is None


class FieldsSession:
    """fields를 받을 수 있는 추론 세션 대역(accepts_fields). 받은 fields를 남긴다."""
    accepts_fields = True

    def __init__(self, inner):
        self.inner, self.fields_seen = inner, []

    def extract(self, request, fields=None):
        self.fields_seen.append(fields)
        return self.inner.extract(request)

    def respond(self, request):
        return self.inner.respond(request)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_graph_sends_the_fields_built_from_the_bank_to_a_session_that_accepts_them():
    rig = Rig()
    rig.release.session = fs = FieldsSession(rig.session)
    rig.start()
    rig.say("음", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
    assert fs.fields_seen == [build_field_specs(rig.bank)]


def test_graph_does_not_send_fields_to_a_session_that_cannot_take_them():
    rig = Rig()
    rig.start()
    assert rig.say("음", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC")).orchestrator_output["turn_failure_signal"] is None      # RecordingSession.extract(request)만 받는다


RESULT = SimpleNamespace(is_fallback=False, protections={})


def test_remote_session_passes_fields_only_when_the_client_supports_them():
    class Client:
        def __init__(self, with_fields):
            self.calls = []
            self._timeout = 1.0
            if with_fields:
                self.extract = self._with
            else:
                self.extract = self._without

        def _with(self, rid, request, flow=None, fields=None):
            self.calls.append((flow, fields))
            return RESULT

        def _without(self, rid, request, flow=None):
            self.calls.append((flow, "no-fields-param"))
            return RESULT
    f, spec = {"order": ["Q1"], "skips": []}, {"fields": [{"slot": "Q1.a", "data_type": "boolean"}]}
    a, b = Client(True), Client(False)
    assert RemoteModelSession(a, "rel").extract({"x": 1}, flow=f, fields=spec) is RESULT and a.calls == [(f, spec)]
    assert RemoteModelSession(b, "rel").extract({"x": 1}, flow=f, fields=spec) is RESULT and b.calls == [(f, "no-fields-param")]       # 지원하지 않는 클라이언트에는 보내지 않는다(CR-020)
