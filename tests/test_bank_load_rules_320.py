"""wo-320: 문항 은행의 칸 규격 규칙이 (1) choice_catalog 칸을 받고 (2) 문진 스키마가 적재 거부 조건을 미리 표현하고 (3) 적재가 계약 QuestionFieldSpec으로 칸 규격을 검증한다 (ADR-0045)."""
import copy

import jsonschema
import pytest
import survey_schema

from .timing import fake_clock
from survey_service.components.field_specs import invalid_field_specs
from survey_service.components.field_specs import build_field_specs
from survey_service.components.link_inference import InferenceServer
from survey_service.components.release_loader import ReleaseLoader
from survey_service.components.release_registry import ReleaseRegistry

from .fakes import FakeReleaseReader
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED

CATALOG = [{"code": "A", "label": "가", "synonyms": []}, {"code": "B", "label": "나", "synonyms": ["둘째"]}]


def _slot(bank, slot_id):
    return next(s for q in bank["questions"] for s in q["slots"] if s["slot_id"] == slot_id)


def _catalog_bank():
    bank = copy.deepcopy(sample_bank())
    s = _slot(bank, "use_pattern")                         # option 칸(Q6_1)
    s["choices"], s["choice_catalog"] = [], copy.deepcopy(CATALOG)
    return bank


def _errors(bank):
    return list(jsonschema.Draft202012Validator(survey_schema.load_schema("QuestionBank")).iter_errors(bank))


def _poll(tmp_path, bank):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", bank, **INSTALLED)
    reader.set_pointer("rel-1")
    reg = ReleaseRegistry()
    loader = ReleaseLoader(reader, reg, InferenceServer("fake"), workdir=tmp_path / "w", installed_versions=INSTALLED, retry_base_seconds=0, field_check=invalid_field_specs, clock=fake_clock())
    return reg, loader, loader.poll_once()


# ---- 1) choice_catalog
def test_catalog_slot_becomes_options_from_the_codes():
    f = {x["slot"]: x for x in build_field_specs(_catalog_bank())["fields"]}["Q6_1.use_pattern"]
    assert f["data_type"] == "option" and f["options"] == ["A", "B"]


def test_a_bank_with_a_choice_catalog_slot_loads(tmp_path):
    bank = _catalog_bank()
    assert _errors(bank) == []
    reg, _, r = _poll(tmp_path, bank)
    assert r.action in ("LOADED", "ACTIVATED") and reg.active_id() == "rel-1"


# ---- 2) 스키마가 적재 거부 조건을 미리 표현한다
def test_schema_requires_the_four_fixed_sentences():
    for key in ("RISK_NOTICE", "CALL_STAFF", "RESUME", "SURVEY_STOPPED"):
        bank = copy.deepcopy(sample_bank())
        bank["session_settings"]["fixed_sentences"] = [f for f in bank["session_settings"]["fixed_sentences"] if f["key"] != key]
        assert _errors(bank), key
    blank = copy.deepcopy(sample_bank())
    next(f for f in blank["session_settings"]["fixed_sentences"] if f["key"] == "RESUME")["sentence"] = "  \t"
    assert _errors(blank)


def test_schema_requires_data_type_and_options_for_option_slots():
    a = copy.deepcopy(sample_bank())
    _slot(a, "diagnosed").pop("data_type")
    b = copy.deepcopy(sample_bank())
    _slot(b, "use_pattern")["choices"] = []
    assert _errors(a) and _errors(b)


def test_schema_ids_follow_the_contract_slot_pattern_and_choices_are_unique():
    q = copy.deepcopy(sample_bank())
    q["questions"][0]["question_id"] = "Q 1"
    s = copy.deepcopy(sample_bank())
    _slot(s, "diagnosed")["slot_id"] = "a.b"
    u = copy.deepcopy(sample_bank())
    _slot(u, "use_pattern")["choices"] = ["X", "X"]
    assert _errors(q) and _errors(s) and _errors(u)


def test_the_sample_bank_and_catalog_bank_are_schema_valid():
    assert _errors(sample_bank()) == [] and _errors(_catalog_bank()) == []


# ---- 3) 적재가 계약 QuestionFieldSpec으로 칸 규격을 검증한다
def test_duplicate_catalog_codes_are_rejected_at_load(tmp_path):
    bank = _catalog_bank()
    _slot(bank, "use_pattern")["choice_catalog"].append({"code": "A", "label": "다", "synonyms": []})      # 스키마는 코드 중복을 표현하지 못한다
    assert _errors(bank) == []
    reg, loader, r = _poll(tmp_path, bank)
    assert r.action == "REJECTED" and r.code == "INVALID_FIELD_SPEC" and reg.active_id() is None
    assert "Q6_1.use_pattern" in loader.status.rejections[-1]["detail"]
