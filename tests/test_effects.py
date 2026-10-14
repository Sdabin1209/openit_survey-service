"""저장소 반영 효과(session_store.md 9-2)를 표의 행마다 확인한다."""
import copy

import pytest

from survey_service.errors import StoreEffectError
from survey_service.store import effects
from .skeleton_bank import skeleton_bank

SLOT = {"question_id": "Q2", "repeat_key": "", "slot_id": "개비"}


def rec(cv="NONE", risk=None, history=(), pending=None, md=None):
    r = {"slot": SLOT, "current_value": cv, "risk_mark": risk, "history": list(history), "pending": pending}
    if md:
        r["value_metadata"] = md
    return r


def dec(action, value=None, pending_id=None, risk=None, md=None):
    d = {"slot": SLOT, "action": action, "value": value, "pending_id": pending_id, "risk_mark": risk}
    if md:
        d["value_metadata"] = md
    return d


def apply(r, d):
    return effects.apply_store_result([r], {"store_decisions": [d], "store_summary": effects.empty_store_result()["store_summary"]})[0]


V10, V20 = {"amount": 10, "unit": "개비"}, {"amount": 20, "unit": "개비"}
MD = {"precision": "approximate", "evidence": []}


def test_first_write():
    r = apply(rec(), dec("FIRST_WRITE", V10, risk="ON_RESULT"))
    assert r["current_value"] == {"answer": V10} and r["history"] == [] and r["risk_mark"] == "ON_RESULT" and r["pending"] is None


def test_first_write_keeps_pending_untouched():
    p = {"pending_id": "p1", "value": V20, "risk_mark": None}
    assert apply(rec(pending=p), dec("FIRST_WRITE", V10))["pending"] == p


def test_new_revision_pushes_previous_answer():
    r = apply(rec({"answer": V10}, risk="IMMEDIATE"), dec("NEW_REVISION", V20))
    assert r["current_value"] == {"answer": V20} and r["history"] == [V10] and r["risk_mark"] is None


def test_new_revision_pushes_previous_with_metadata():
    r = apply(rec({"answer": V10}, md=MD), dec("NEW_REVISION", V20))
    assert r["history"] == [{"value": V10, "value_metadata": MD}] and "value_metadata" not in r


def test_new_revision_after_unknown_pushes_nothing():
    assert apply(rec("UNKNOWN_RECORD"), dec("NEW_REVISION", V20))["history"] == []


@pytest.mark.parametrize("action,cv", [("RECORD_UNKNOWN", "UNKNOWN_RECORD"), ("RECORD_REFUSAL", "REFUSAL_RECORD")])
def test_unknown_refusal(action, cv):
    r = apply(rec({"answer": V10}, risk="IMMEDIATE", md=MD), dec(action))
    assert r["current_value"] == cv and r["history"] == [{"value": V10, "value_metadata": MD}] and r["risk_mark"] is None
    assert "value_metadata" not in r
    # 모름·거절은 답이 아니라 이력에 쌓지 않는다
    assert apply(rec("UNKNOWN_RECORD"), dec(action))["history"] == []


def test_set_pending_replaces_and_keeps_current():
    old = {"pending_id": "p0", "value": V10, "risk_mark": None}
    r = apply(rec({"answer": V10}, pending=old), dec("SET_PENDING", V20, "p1", "ON_RESULT", MD))
    assert r["current_value"] == {"answer": V10} and r["history"] == []
    assert r["pending"] == {"pending_id": "p1", "value": V20, "risk_mark": "ON_RESULT", "value_metadata": MD}


def test_accept_pending():
    p = {"pending_id": "p1", "value": V20, "risk_mark": "ON_RESULT", "value_metadata": MD}
    r = apply(rec({"answer": V10}, pending=p), dec("ACCEPT_PENDING", V20, "p1"))
    assert r["current_value"] == {"answer": V20} and r["history"] == [V10] and r["pending"] is None
    assert r["risk_mark"] == "ON_RESULT" and r["value_metadata"] == MD


def test_accept_pending_mismatch_rejected():
    p = {"pending_id": "p1", "value": V20, "risk_mark": None}
    with pytest.raises(StoreEffectError):
        apply(rec({"answer": V10}, pending=p), dec("ACCEPT_PENDING", V20, "other"))
    with pytest.raises(StoreEffectError):
        apply(rec({"answer": V10}), dec("ACCEPT_PENDING", V20, "p1"))


def test_drop_pending():
    p = {"pending_id": "p1", "value": V20, "risk_mark": None}
    r = apply(rec({"answer": V10}, risk="IMMEDIATE", pending=p), dec("DROP_PENDING", pending_id="p1"))
    assert r["pending"] is None and r["current_value"] == {"answer": V10} and r["history"] == [] and r["risk_mark"] == "IMMEDIATE"


def test_invalidate():
    p = {"pending_id": "p1", "value": V20, "risk_mark": None}
    r = apply(rec({"answer": V10}, risk="IMMEDIATE", pending=p, md=MD), dec("INVALIDATE"))
    assert r["current_value"] == "NONE" and r["history"] == [{"value": V10, "value_metadata": MD}]
    assert r["pending"] is None and r["risk_mark"] is None and "value_metadata" not in r


def test_unknown_slot_rejected():
    bad = {"slot": {"question_id": "Q9", "repeat_key": "", "slot_id": "x"}, "action": "FIRST_WRITE", "value": "a", "pending_id": None, "risk_mark": None}
    with pytest.raises(StoreEffectError):
        effects.apply_store_result([rec()], {"store_decisions": [bad], "store_summary": {}})


def test_input_not_mutated():
    r0 = rec({"answer": V10})
    snap = copy.deepcopy(r0)
    apply(r0, dec("NEW_REVISION", V20))
    assert r0 == snap


def update(**kw):
    base = {"last_directive": {"action": "ASK_NEXT"}, "open_requests": {"correction_requests": [], "stop_requested": False, "deferred": []},
            "skipped_add": [], "skipped_remove": [], "confirm_stopped_add": [], "confirm_stopped_remove": [],
            "session_status": None, "pause_history_add": []}
    base.update(kw)
    return base


def prog(**kw):
    base = effects.initial_state("S", skeleton_bank())["progress_record"]
    base.update(kw)
    return base


def test_progress_replaces_directive_and_open_requests():
    u = update(last_directive={"action": "ASK_MISSING"}, open_requests={"correction_requests": [{"target": None}], "stop_requested": True, "deferred": []})
    p = effects.apply_progress_update(prog(), u)
    assert p["last_directive"] == {"action": "ASK_MISSING"} and p["open_requests"]["stop_requested"] is True


def test_skipped_add_replace_remove():
    p = prog(skipped=[{"question_id": "Q1", "reason": "UNKNOWN_ACCEPTED"}, {"question_id": "Q2", "reason": "REFUSAL_ACCEPTED"}])
    u = update(skipped_add=[{"question_id": "Q1", "reason": "ATTEMPTS_EXCEEDED"}, {"question_id": "Q5", "reason": "UNKNOWN_ACCEPTED"}],
               skipped_remove=["Q2"])
    out = effects.apply_progress_update(p, u)["skipped"]
    assert out == [{"question_id": "Q1", "reason": "ATTEMPTS_EXCEEDED"}, {"question_id": "Q5", "reason": "UNKNOWN_ACCEPTED"}]


def test_skipped_remove_before_add():
    p = prog(skipped=[{"question_id": "Q1", "reason": "UNKNOWN_ACCEPTED"}])
    u = update(skipped_add=[{"question_id": "Q1", "reason": "REFUSAL_ACCEPTED"}], skipped_remove=["Q1"])
    assert effects.apply_progress_update(p, u)["skipped"] == [{"question_id": "Q1", "reason": "REFUSAL_ACCEPTED"}]


def test_confirm_stopped_add_remove_dedupe():
    s1 = {"question_id": "Q2", "repeat_key": "", "slot_id": "개비"}
    s2 = {"question_id": "Q5", "repeat_key": "", "slot_id": "시간"}
    p = prog(confirm_stopped=[s1])
    out = effects.apply_progress_update(p, update(confirm_stopped_add=[s1, s2], confirm_stopped_remove=[]))["confirm_stopped"]
    assert out == [s1, s2]
    out = effects.apply_progress_update(p, update(confirm_stopped_remove=[s1]))["confirm_stopped"]
    assert out == []


def test_status_and_pause_history():
    p = prog(pause_history=[{"kind": "PAUSE", "reasons": []}])
    out = effects.apply_progress_update(p, update(session_status="PAUSED_RISK", pause_history_add=[{"kind": "RESUME", "reasons": []}]))
    assert out["session_status"] == "PAUSED_RISK" and [e["kind"] for e in out["pause_history"]] == ["PAUSE", "RESUME"]
    assert effects.apply_progress_update(p, update())["session_status"] == "IN_PROGRESS"


def test_initial_state_and_planner_record_replace():
    st = effects.initial_state("S", skeleton_bank())
    assert st["progress_record"]["session_status"] == "IN_PROGRESS" and st["dialogue_log"] == [] and st["dispatch_log"] == []
    assert all(r["current_value"] == "NONE" and r["history"] == [] and r["pending"] is None for r in st["answer_records"])
    new_plan = {"attempt_counts": [{"question_id": "Q1", "count": 1}], "confirm_counts": [], "unknown_counts": [], "refusal_counts": [],
                "unrecognized_streak": 1, "ask_counts": []}
    out = effects.apply_writes(st, {"store_result": effects.empty_store_result(), "progress_record_update": update(), "planner_record_update": new_plan})
    assert out["planner_record"] == new_plan and st["planner_record"]["unrecognized_streak"] == 0
