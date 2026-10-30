"""서비스 고유 검사(ADR-0010, R7): 서버가 보증하지 못하는 두 가지만 본다. 입력 ExtractionCheckQuery -> 출력 ExtractionViolations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional


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


@dataclass(frozen=True)
class Violation:
    """서비스 쪽 검사의 위반(code, path, message. 값 없음)."""
    code: str
    path: str
    message: str


def check_slots(output: Mapping, records: list) -> list[Violation]:
    """저장하는 값(save=true)의 칸이 문항 세트의 슬롯에 있는가. 없으면 지어내지 않고 위반으로 본다(재시도 대상)."""
    have = {_slot_key(r["slot"]) for r in records}
    out = []
    for i, v in enumerate(output["values"]):
        if v["save"] and _slot_key(_slot_ref(v["key"])) not in have:
            out.append(Violation("UNKNOWN_SLOT", f"values/{i}/key", "저장하려는 칸이 문항 세트에 없다"))
    return out


def _family(item: str) -> str:
    return item.split("_", 1)[0].split(".", 1)[0]


def check_current_item(output: Mapping, doc: Mapping, flow: Optional[Any] = None) -> list[Violation]:
    """서버가 보증하지 못하는 것만 서비스가 본다(R7, ADR-0035, ADR-0043): 저장하는 값의 칸이 현재 문항이어야 한다(정정은 예외: status CORRECTION 또는 action CORRECT).
    서버(model-runtime)는 요청의 흐름 표와 칸 규격은 알지만 세션의 현재 문항 진행은 모른다. 자료형·범위·선택지는 서버가 fields로 항상 보증한다(계약 3.0.0)."""
    out: list[Violation] = []
    cur_q = doc["progress_record"]["current_question_id"]
    # 한 발화가 문항과 그 후속 문항(Q4와 Q4_1, Q1.D01과 Q1.D02)을 함께 채우는 것은 정상이다: 현재 문항과 next_item의 같은 계열(Q4_1 -> Q4, Q1.D01 -> Q1)까지 허용한다
    fams = {_family(cur_q)} | ({_family(output["next_item"])} if output.get("next_item") not in (None, "END") else set())
    span: set = set()                    # 한 발화가 여러 문항을 한꺼번에 답하면(예: 운동 문항 세 개를 '안 해요') 현재 문항에서 next_item 사이의 문항도 정상이다 (C1 학습 데이터 대조에서 확인)
    if flow is not None and cur_q in flow.table.order:
        order = list(flow.table.order)
        i = order.index(cur_q)
        nxt = output.get("next_item")
        j = len(order) - 1 if nxt == "END" else (order.index(nxt) if nxt in order else i)
        span = set(order[i:max(i, j) + 1])
    is_corr_action = output.get("action") == "CORRECT"
    for i, v in enumerate(output["values"]):
        if not v["save"]:
            continue
        ref = _slot_ref(v["key"])
        if _family(ref["question_id"]) not in fams and ref["question_id"] not in span and not (is_corr_action or v["status"] == "CORRECTION"):
            out.append(Violation("OUT_OF_CURRENT_ITEM", f"values/{i}/key", "현재 문항이 아닌 칸을 정정이 아닌데 저장하려 한다"))
    return out


