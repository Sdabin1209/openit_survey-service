"""추출 결과(save=true)에서 저장·진행 계획(TurnWriteSet)을 만든다. 입력 WritePlanQuery -> 출력 TurnWriteSet. 입력 문서는 바꾸지 않는다.
"""
from __future__ import annotations

import copy
import re
from typing import Mapping


def _slot_key(slot: Mapping) -> tuple:
    return (slot["question_id"], slot.get("repeat_key", ""), slot["slot_id"])


def _model_key(slot: Mapping) -> str:
    """SlotRef -> 모델 키(`Q7_1[soju].amount`). 왜 복제인가: 컴포넌트끼리 import 금지(DESIGN 2절, 한계 L2)."""
    rk = slot.get("repeat_key", "")
    return f"{slot['question_id']}[{rk}].{slot['slot_id']}" if rk else f"{slot['question_id']}.{slot['slot_id']}"


def _slot_ref(key: str) -> dict:
    """모델 키 -> SlotRef. 종류 괄호가 없으면 repeat_key는 빈 문자열(한계 L2: 복제)."""
    head, slot_id = key.rsplit(".", 1)
    item, _, rest = head.partition("[")
    return {"question_id": item, "repeat_key": rest[:-1] if rest else "", "slot_id": slot_id}


def _answers_by_key(records: list) -> dict:
    """저장된 값(answer)이 있는 칸: 모델 키 -> (값, precision)(한계 L2: 복제)."""
    out = {}
    for r in records:
        cv = r["current_value"]
        if isinstance(cv, dict) and "answer" in cv:
            out[_model_key(r["slot"])] = (cv["answer"], r.get("precision", "unspecified"))
    return out


def _item_of(key: str) -> str:
    """`Q7_1[soju].amount` -> `Q7_1`(한계 L2: 복제)."""
    return key.rsplit(".", 1)[0].split("[")[0]


def _slot_name(key: str) -> str:
    """`Q7_1[soju].amount` -> `Q7_1.amount`(한계 L2: 복제)."""
    return re.sub(r"\[[^\]]*\]", "", key)


SKIP_REASON = {"REQUEST_SKIP": "REQUEST_SKIP", "REFUSED": "REFUSED"}   # SKIP 사유 -> 건너뜀 사유. HOLD는 UNCERTAIN


def plan_writes(doc: Mapping, output: Mapping) -> dict:
    """TurnWriteSet. 입력 문서는 바꾸지 않는다."""
    answers = _answers_by_key(doc["answer_records"])
    updates, saved_keys = [], set()
    for v in output["values"]:
        if not v["save"]:
            continue
        saved_keys.add(v["key"])
        cur = answers.get(v["key"])
        if cur is not None and cur[0] == v["value"] and isinstance(cur[0], bool) == isinstance(v["value"], bool) and cur[1] == v["precision"]:
            continue                      # 같은 값을 다시 말했다: 바꿀 것이 없다
        updates.append({"slot": _slot_ref(v["key"]), "action": "FIRST_WRITE" if cur is None else "NEW_REVISION",
                        "value": copy.deepcopy(v["value"]), "precision": v["precision"]})
    nxt = output["next_item"]
    prog = doc["progress_record"]
    open_slots = {o["key"]: o["status"] for o in prog["open_slots"]}
    for k in saved_keys:
        open_slots.pop(k, None)
    for v in output["values"]:
        if not v["save"] and v["status"] in ("MISSING", "UNCLEAR"):
            open_slots[v["key"]] = v["status"]
    open_list = [{"key": k, "status": s} for k, s in open_slots.items() if nxt != "END" and _item_of(k) == nxt]
    skipped_add, skipped_remove = [], sorted({_item_of(k) for k in saved_keys if any(e["question_id"] == _item_of(k) for e in prog["skipped"])})
    if output["action"] in ("SKIP", "HOLD"):
        reason = "UNCERTAIN" if output["action"] == "HOLD" else SKIP_REASON[output["reason"]]
        for item in dict.fromkeys(_item_of(t) for t in output.get("targets", [])):
            skipped_add.append({"question_id": item, "reason": reason})
            if item in skipped_remove:
                skipped_remove.remove(item)
    update = {"current_question_id": nxt, "open_slots": open_list, "skipped_add": skipped_add, "skipped_remove": skipped_remove,
              "session_status": "ENDED_COMPLETE" if nxt == "END" else None, "pause_history_add": []}
    return {"answer_updates": updates, "progress_record_update": update}


