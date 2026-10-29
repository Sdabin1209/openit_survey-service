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
from survey_service.store import effects

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


def test_remote_session_passes_flow_and_fields_to_the_contract_client():
    class Client:
        _timeout = 1.0

        def __init__(self):
            self.calls = []

        def extract(self, rid, request, flow=None, fields=None):
            self.calls.append((flow, fields))
            return RESULT
    f, spec = {"order": ["Q1"], "skips": []}, {"fields": [{"slot": "Q1.a", "data_type": "boolean"}]}
    c = Client()
    assert RemoteModelSession(c, "rel").extract({"x": 1}, flow=f, fields=spec) is RESULT
    assert RemoteModelSession(c, "rel").extract({"x": 1}, flow=f) is RESULT              # 칸 규격이 없는 문항 은행: fields를 보내지 않는다
    assert c.calls == [(f, spec), (f, None)]


def test_reference_server_gets_fields_and_returns_the_dataset_answer_shape_for_an_out_of_range_value(tmp_path):
    """계약 기준 서버(RuntimeReferenceServer)는 fields가 오면 값 규칙을 적용한다: Q10.days=8(범위 0~7)은 데이터셋이 낸 정답 모양(REASK/OUT_OF_RANGE, 값 null, UNCLEAR)으로 돌아온다."""
    from .test_inference_client import Env
    e = Env(tmp_path)
    try:
        assert e.loader.poll_once().action == "LOADED"
        e.orch.store.create("S2", "rel-1", effects.initial_state("S2", sample_bank(), "Q10"))      # 마지막 문항에서 시작: REASK의 next_item은 현재 문항이어야 한다
        e.srv.extract_fn = lambda req: ext("NEXT", "END", val("Q10.days", 8))
        o = e.turn("여덟 일", sid="S2")
        body = e.srv.extract_bodies[-1]
        assert body["fields"] == build_field_specs(sample_bank()) and list(validator("ExtractRequest").iter_errors(body)) == []
        out = e.orch.store.load("S2")["state"]["dialogue_log"][-1]["extract_output"]
        assert out["action"] == "REASK" and out["reason"] == "OUT_OF_RANGE" and out["targets"] == ["Q10.days"] and out["next_item"] == "Q10"
        v = next(x for x in out["values"] if x["key"] == "Q10.days")
        assert v["value"] is None and v["status"] == "UNCLEAR" and v["save"] is False and v["precision"] == "unspecified"
        assert o.orchestrator_output["turn_failure_signal"] is None
    finally:
        e.close()
