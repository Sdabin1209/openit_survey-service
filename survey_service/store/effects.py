"""저장소 반영 효과(docs/rules/session_store.md 9-2)를 판단 없이 적용하는 순수 함수.

입력 문서는 바꾸지 않고 새 문서를 돌려준다.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

from ..errors import StoreEffectError

EMPTY_STORE_SUMMARY = {"changed_slots": [], "unknown_refused_records": [], "created_pendings": [],
                       "resolved_pendings": [], "invalidated_questions": []}


def empty_store_result() -> dict:
    return {"store_decisions": [], "store_summary": copy.deepcopy(EMPTY_STORE_SUMMARY)}


def slot_key(slot: Mapping) -> tuple:
    return (slot["question_id"], slot.get("repeat_key", ""), slot["slot_id"])


def ordered_questions(bank: Mapping) -> list[dict]:
    return sorted(bank["questions"], key=lambda q: q["order"])


def initial_answer_records(bank: Mapping) -> list[dict]:
    """모든 슬롯을 '없음'으로. 문항 order 순, 반복 키마다, 슬롯 정의 순."""
    out = []
    for q in ordered_questions(bank):
        for rk in (q.get("repeat_keys") or [""]):
            for s in q["slots"]:
                out.append({"slot": {"question_id": q["question_id"], "repeat_key": rk, "slot_id": s["slot_id"]},
                            "current_value": "NONE", "risk_mark": None, "history": [], "pending": None})
    return out


def initial_state(session_id: str, bank: Mapping) -> dict:
    """세션 정보 생성 효과(9-2 갱신·생성 2항)."""
    return {
        "session_info": {"session_id": session_id, "question_bank_version": bank["version"]},
        "answer_records": initial_answer_records(bank),
        "progress_record": {
            "last_directive": None,
            "open_requests": {"correction_requests": [], "stop_requested": False, "deferred": []},
            "skipped": [], "confirm_stopped": [], "session_status": "IN_PROGRESS", "pause_history": [],
        },
        "planner_record": {"attempt_counts": [], "confirm_counts": [], "unknown_counts": [], "refusal_counts": [],
                           "unrecognized_streak": 0, "ask_counts": []},
        "dialogue_log": [],
        "dispatch_log": [],
    }


def _previous_answer_entry(rec: Mapping) -> Any:
    """이력에 쌓을 이전 답. 현재값이 값(answer)일 때만(모름·거절·없음은 쌓지 않는다)."""
    cv = rec["current_value"]
    if isinstance(cv, dict) and "answer" in cv:
        md = rec.get("value_metadata")
        return {"value": cv["answer"], "value_metadata": copy.deepcopy(md)} if md else copy.deepcopy(cv["answer"])
    return None


def _push_history(rec: dict) -> None:
    prev = _previous_answer_entry(rec)
    if prev is not None:
        rec["history"].append(prev)


def _set_answer(rec: dict, value: Any, risk_mark: Any, metadata: Any) -> None:
    rec["current_value"] = {"answer": copy.deepcopy(value)}
    rec["risk_mark"] = risk_mark
    if metadata:
        rec["value_metadata"] = copy.deepcopy(metadata)
    else:
        rec.pop("value_metadata", None)


def apply_store_result(records: list, store_result: Mapping) -> list:
    """저장 결정(StoreResult.store_decisions)을 응답 기록에 반영한다(9-2 첫 번째 표)."""
    recs = copy.deepcopy(records)
    index = {slot_key(r["slot"]): r for r in recs}
    for d in store_result["store_decisions"]:
        rec = index.get(slot_key(d["slot"]))
        if rec is None:
            raise StoreEffectError("저장 결정의 슬롯이 응답 기록에 없다")
        action = d["action"]
        if action == "FIRST_WRITE":
            _set_answer(rec, d["value"], d["risk_mark"], d.get("value_metadata"))
        elif action == "NEW_REVISION":
            _push_history(rec)
            _set_answer(rec, d["value"], d["risk_mark"], d.get("value_metadata"))
        elif action in ("RECORD_UNKNOWN", "RECORD_REFUSAL"):
            _push_history(rec)
            rec["current_value"] = "UNKNOWN_RECORD" if action == "RECORD_UNKNOWN" else "REFUSAL_RECORD"
            rec["risk_mark"] = None
            rec.pop("value_metadata", None)
        elif action == "SET_PENDING":
            pend = {"pending_id": d["pending_id"], "value": copy.deepcopy(d["value"]), "risk_mark": d["risk_mark"]}
            if d.get("value_metadata"):
                pend["value_metadata"] = copy.deepcopy(d["value_metadata"])
            rec["pending"] = pend
        elif action == "ACCEPT_PENDING":
            pend = rec["pending"]
            if pend is None or pend["pending_id"] != d.get("pending_id"):
                raise StoreEffectError("받아들일 확인 대기가 없거나 ID가 다르다")
            _push_history(rec)
            _set_answer(rec, pend["value"], pend["risk_mark"], pend.get("value_metadata"))
            rec["pending"] = None
        elif action == "DROP_PENDING":
            pend = rec["pending"]
            if pend is None or pend["pending_id"] != d.get("pending_id"):
                raise StoreEffectError("버릴 확인 대기가 없거나 ID가 다르다")
            rec["pending"] = None
        elif action == "INVALIDATE":
            _push_history(rec)
            rec["current_value"] = "NONE"
            rec["risk_mark"] = None
            rec["pending"] = None
            rec.pop("value_metadata", None)
        else:
            raise StoreEffectError(f"알 수 없는 저장 동작: {action}")
    return recs


def apply_progress_update(progress: Mapping, update: Mapping | None) -> dict:
    """진행 기록 갱신(9-2 갱신 1항). 해제를 추가보다 먼저 한다."""
    prog = copy.deepcopy(progress)
    if update is None:
        return prog
    prog["last_directive"] = copy.deepcopy(update["last_directive"])
    prog["open_requests"] = copy.deepcopy(update["open_requests"])
    removed = set(update["skipped_remove"])
    prog["skipped"] = [e for e in prog["skipped"] if e["question_id"] not in removed]
    for add in update["skipped_add"]:
        for e in prog["skipped"]:
            if e["question_id"] == add["question_id"]:
                e["reason"] = add["reason"]
                break
        else:
            prog["skipped"].append(copy.deepcopy(add))
    rm = {slot_key(s) for s in update["confirm_stopped_remove"]}
    prog["confirm_stopped"] = [s for s in prog["confirm_stopped"] if slot_key(s) not in rm]
    have = {slot_key(s) for s in prog["confirm_stopped"]}
    for s in update["confirm_stopped_add"]:
        if slot_key(s) not in have:
            prog["confirm_stopped"].append(copy.deepcopy(s))
            have.add(slot_key(s))
    if update["session_status"] is not None:
        prog["session_status"] = update["session_status"]
    prog["pause_history"] = prog["pause_history"] + copy.deepcopy(update["pause_history_add"])
    return prog


def apply_writes(doc: Mapping, writes: Mapping) -> dict:
    """한 턴의 쓰기(TurnWriteSet) 전체를 문서에 반영한다. 하나라도 반영할 수 없으면 StoreEffectError."""
    new = copy.deepcopy(doc)
    new["answer_records"] = apply_store_result(doc["answer_records"], writes["store_result"])
    new["progress_record"] = apply_progress_update(doc["progress_record"], writes.get("progress_record_update"))
    if writes.get("planner_record_update") is not None:
        new["planner_record"] = copy.deepcopy(writes["planner_record_update"])
    return new
