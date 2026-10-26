"""저장소 반영 효과를 판단 없이 적용하는 순수 함수(D74 구조). 입력 문서는 바꾸지 않고 새 문서를 돌려준다.

저장 값은 AnswerUpdate(첫 저장 FIRST_WRITE, 정정 NEW_REVISION)로만 들어온다. 어떤 값을 저장할지는 추출 결과(save=true)와 서비스의
`extraction.plan_writes`가 정하고, 여기서는 효과만 적용한다. 반영할 수 없는 쓰기는 StoreEffectError로 거부한다(쓰기 전체가 반영되지 않는다).
"""
from __future__ import annotations

import copy
from typing import Any, Mapping, Optional

from ..errors import StoreEffectError


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
                            "current_value": "NONE", "risk_mark": None, "history": []})
    return out


def initial_state(session_id: str, bank: Mapping, first_question_id: str) -> dict:
    """세션 정보 생성 효과."""
    return {
        "session_info": {"session_id": session_id, "question_bank_version": bank["version"]},
        "answer_records": initial_answer_records(bank),
        "progress_record": {"current_question_id": first_question_id, "open_slots": [], "skipped": [],
                            "session_status": "IN_PROGRESS", "pause_history": []},
        "dialogue_log": [],
        "dispatch_log": [],
    }


def model_key(slot: Mapping) -> str:
    """SlotRef -> 모델 키(`Q7_1[soju].amount`)."""
    rk = slot.get("repeat_key", "")
    return f"{slot['question_id']}[{rk}].{slot['slot_id']}" if rk else f"{slot['question_id']}.{slot['slot_id']}"


def slot_ref(key: str) -> dict:
    """모델 키 -> SlotRef. 종류 괄호가 없으면 repeat_key는 빈 문자열."""
    head, slot_id = key.rsplit(".", 1)
    item, _, rest = head.partition("[")
    return {"question_id": item, "repeat_key": rest[:-1] if rest else "", "slot_id": slot_id}


def answers_by_key(records: list) -> dict:
    """저장된 값(answer)이 있는 칸: 모델 키 -> (값, precision)."""
    out = {}
    for r in records:
        cv = r["current_value"]
        if isinstance(cv, dict) and "answer" in cv:
            out[model_key(r["slot"])] = (cv["answer"], r.get("precision", "unspecified"))
    return out


def apply_answer_updates(records: list, updates: list) -> list:
    recs = copy.deepcopy(records)
    index = {slot_key(r["slot"]): r for r in recs}
    for u in updates:
        rec = index.get(slot_key(u["slot"]))
        if rec is None:
            raise StoreEffectError("저장할 슬롯이 응답 기록에 없다")
        cv = rec["current_value"]
        has = isinstance(cv, dict) and "answer" in cv
        if u["action"] == "FIRST_WRITE":
            if has:
                raise StoreEffectError("이미 값이 있는 칸의 첫 저장")
        elif u["action"] == "NEW_REVISION":
            if not has:
                raise StoreEffectError("값이 없는 칸의 정정")
            entry = {"value": copy.deepcopy(cv["answer"]), "precision": rec.get("precision", "unspecified")}
            rec["history"].append(entry)
        else:
            raise StoreEffectError(f"알 수 없는 저장 동작: {u['action']}")
        rec["current_value"] = {"answer": copy.deepcopy(u["value"])}
        rec["precision"] = u["precision"]
    return recs


def apply_progress_update(progress: Mapping, update: Optional[Mapping]) -> dict:
    prog = copy.deepcopy(progress)
    if update is None:
        return prog
    prog["current_question_id"] = update["current_question_id"]
    prog["open_slots"] = copy.deepcopy(update["open_slots"])
    removed = set(update["skipped_remove"])
    prog["skipped"] = [e for e in prog["skipped"] if e["question_id"] not in removed]
    for add in update["skipped_add"]:
        for e in prog["skipped"]:
            if e["question_id"] == add["question_id"]:
                e["reason"] = add["reason"]
                break
        else:
            prog["skipped"].append(copy.deepcopy(add))
    if update["session_status"] is not None:
        prog["session_status"] = update["session_status"]
    prog["pause_history"] = prog["pause_history"] + copy.deepcopy(update["pause_history_add"])
    return prog


def apply_writes(doc: Mapping, writes: Mapping) -> dict:
    """한 턴의 쓰기(TurnWriteSet) 전체를 문서에 반영한다. 하나라도 반영할 수 없으면 StoreEffectError."""
    new = copy.deepcopy(doc)
    new["answer_records"] = apply_answer_updates(doc["answer_records"], writes["answer_updates"])
    new["progress_record"] = apply_progress_update(doc["progress_record"], writes.get("progress_record_update"))
    return new
