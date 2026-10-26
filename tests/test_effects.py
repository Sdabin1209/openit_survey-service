"""저장소 반영 효과(survey_service/store/effects.py): 판단 없이 효과만 적용하는 순수 함수."""
import copy

import jsonschema
import pytest

import survey_schema

from survey_service.errors import StoreEffectError
from survey_service.store import effects

from .sample_bank import sample_bank

SS = jsonschema.Draft202012Validator(survey_schema.load_schema("SessionStore"))


def doc():
    return effects.initial_state("S1", sample_bank(), "Q1.D01")


def upd(key, value, action="FIRST_WRITE", precision="exact"):
    return {"slot": effects.slot_ref(key), "action": action, "value": value, "precision": precision}


def prog(**kw):
    base = {"current_question_id": "Q1.D02", "open_slots": [], "skipped_add": [], "skipped_remove": [], "session_status": None,
            "pause_history_add": []}
    base.update(kw)
    return base


def test_initial_state_is_valid_and_all_slots_none():
    d = doc()
    assert not list(SS.iter_errors(d))
    assert all(r["current_value"] == "NONE" and r["history"] == [] for r in d["answer_records"])
    # 반복 키(술 종류)마다 칸이 있다
    keys = {effects.model_key(r["slot"]) for r in d["answer_records"]}
    assert "Q7_1[soju].amount" in keys and "Q7_2[makgeolli].unit" in keys and "Q1.D01.diagnosed" in keys
    assert d["progress_record"] == {"current_question_id": "Q1.D01", "open_slots": [], "skipped": [], "session_status": "IN_PROGRESS",
                                    "pause_history": []}


@pytest.mark.parametrize("key,ref", [("Q7_1[soju].amount", {"question_id": "Q7_1", "repeat_key": "soju", "slot_id": "amount"}),
                                     ("Q1.D03.on_medication", {"question_id": "Q1.D03", "repeat_key": "", "slot_id": "on_medication"}),
                                     ("Q10.days", {"question_id": "Q10", "repeat_key": "", "slot_id": "days"})])
def test_key_and_slot_ref_round_trip(key, ref):
    assert effects.slot_ref(key) == ref and effects.model_key(ref) == key


def test_first_write_and_new_revision_with_history():
    d = doc()
    d1 = effects.apply_writes(d, {"answer_updates": [upd("Q1.D01.diagnosed", True), upd("Q7_1[soju].amount", 2.5, precision="approximate")],
                                  "progress_record_update": prog()})
    assert not list(SS.iter_errors(d1))
    assert d["answer_records"] == doc()["answer_records"]                      # 입력 문서는 바뀌지 않는다
    assert effects.answers_by_key(d1["answer_records"]) == {"Q1.D01.diagnosed": (True, "exact"), "Q7_1[soju].amount": (2.5, "approximate")}
    d2 = effects.apply_writes(d1, {"answer_updates": [upd("Q1.D01.diagnosed", False, "NEW_REVISION")], "progress_record_update": None})
    r = [x for x in d2["answer_records"] if effects.model_key(x["slot"]) == "Q1.D01.diagnosed"][0]
    assert r["current_value"] == {"answer": False} and r["history"] == [{"value": True, "precision": "exact"}]
    assert not list(SS.iter_errors(d2))
    assert d2["progress_record"] == d1["progress_record"]                      # 진행 갱신이 없으면 그대로


def test_bool_and_numbers_are_kept_distinct():
    d = effects.apply_writes(doc(), {"answer_updates": [upd("Q8_1.days", 0), upd("Q4.answer", False)], "progress_record_update": None})
    a = effects.answers_by_key(d["answer_records"])
    assert a["Q8_1.days"][0] == 0 and a["Q8_1.days"][0] is not False and a["Q4.answer"][0] is False


@pytest.mark.parametrize("bad", [
    upd("Q1.D01.diagnosed", True, "NEW_REVISION"),                                  # 값이 없는 칸의 정정
    {"slot": {"question_id": "Q1.D01", "repeat_key": "", "slot_id": "nope"}, "action": "FIRST_WRITE", "value": 1, "precision": "exact"},
    upd("Q1.D01.diagnosed", True, "RECORD_UNKNOWN"),
])
def test_unappliable_writes_are_rejected(bad):
    with pytest.raises(StoreEffectError):
        effects.apply_writes(doc(), {"answer_updates": [bad], "progress_record_update": None})


def test_first_write_over_existing_value_is_rejected():
    d = effects.apply_writes(doc(), {"answer_updates": [upd("Q1.D01.diagnosed", True)], "progress_record_update": None})
    with pytest.raises(StoreEffectError):
        effects.apply_writes(d, {"answer_updates": [upd("Q1.D01.diagnosed", False)], "progress_record_update": None})


def test_progress_update_skipped_remove_before_add_and_status():
    d = effects.apply_writes(doc(), {"answer_updates": [], "progress_record_update": prog(
        skipped_add=[{"question_id": "Q1.D01", "reason": "REQUEST_SKIP"}], open_slots=[{"key": "Q1.D02.diagnosed", "status": "MISSING"}])})
    assert d["progress_record"]["skipped"] == [{"question_id": "Q1.D01", "reason": "REQUEST_SKIP"}]
    assert d["progress_record"]["open_slots"] == [{"key": "Q1.D02.diagnosed", "status": "MISSING"}]
    d = effects.apply_writes(d, {"answer_updates": [], "progress_record_update": prog(
        skipped_remove=["Q1.D01"], skipped_add=[{"question_id": "Q1.D01", "reason": "REFUSED"}], session_status="PAUSED_RISK",
        pause_history_add=[{"kind": "PAUSE", "reasons": [{"kind": "RISK_SIGNAL", "evidence": ["x"]}]}])})
    assert d["progress_record"]["skipped"] == [{"question_id": "Q1.D01", "reason": "REFUSED"}]     # 해제를 먼저 한다
    assert d["progress_record"]["session_status"] == "PAUSED_RISK" and len(d["progress_record"]["pause_history"]) == 1
    assert not list(SS.iter_errors(d))
    again = effects.apply_writes(d, {"answer_updates": [], "progress_record_update": prog(skipped_add=[{"question_id": "Q1.D01", "reason": "UNCERTAIN"}])})
    assert again["progress_record"]["skipped"] == [{"question_id": "Q1.D01", "reason": "UNCERTAIN"}]       # 중복 없이 사유만 바뀐다
