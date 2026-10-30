"""모델 추출 입력(ActionExtractionInput) 조립. 입력 ExtractRequestSource -> 출력 ExtractInput. 세션 문서·표에서 정해진 것을 고르고 옮길 뿐 판단하지 않는다.

state는 지금 문항의 저장 값 -> 직전 문항(건너뛴 문항은 뺀다)과 참조 원천 문항의 저장 값 -> 지금 문항의 MISSING·UNCLEAR 순서다(학습 데이터 규칙, 표본에서 읽은 것이라 미확정).
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from ..wiring.errors import TurnInputError


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


# ---------------------------------------------------------------- A 입력

def last_response_text(doc: Mapping) -> Optional[str]:
    """직전 응답 전체 문장(A 입력의 current_question). 대화 로그의 마지막 응답이다. 없으면 None."""
    for e in reversed(doc["dialogue_log"]):
        if e.get("response_text"):
            return e["response_text"]
    return None


def _saved_by_slot(answers: Mapping[str, tuple]) -> dict:
    """건너뛰기 표가 보는 형태: 칸 이름(종류 괄호 없음) -> 저장된 값."""
    return {_slot_name(k): v[0] for k, v in answers.items()}


def build_state(doc: Mapping, flow: Any) -> dict:
    """A 입력의 state(삽입 순서가 규칙이다, 위 설명)."""
    prog = doc["progress_record"]
    q = prog["current_question_id"]
    answers = _answers_by_key(doc["answer_records"])
    state: dict[str, dict] = {}

    def add_item(item: str) -> None:
        for k in sorted((k for k in answers if _item_of(k) == item), key=flow.sort_key):
            state.setdefault(k, {"value": answers[k][0], "status": "ANSWERED"})

    add_item(q)
    others = []
    prev = flow.previous_item(q, _saved_by_slot(answers))
    for it in ([prev] if prev else []) + list(flow.reference_sources.get(q, ())):
        if it not in others and it != q:
            others.append(it)
    for it in others:
        add_item(it)
    for o in sorted(prog["open_slots"], key=lambda s: flow.sort_key(s["key"])):
        if _item_of(o["key"]) == q:
            state.setdefault(o["key"], {"value": None, "status": o["status"]})
    return state


def build_extract_input(doc: Mapping, bank: Any, flow: Any, utterance: str) -> dict:
    """ActionExtractionInput. 최상위 키는 알파벳 순(학습 데이터와 같다)."""
    q = doc["progress_record"]["current_question_id"]
    cq = last_response_text(doc)
    if cq is None:                      # 직전 응답이 없다(응답 실패 등): 문항 은행의 질문 문장
        cq = bank.sentence(q)
        if cq is None:
            raise TurnInputError("NO_QUESTION_SENTENCE")
    return {"current_question": cq, "question_id": q, "state": build_state(doc, flow), "user_utterance": utterance}


