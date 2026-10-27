"""D74 구조의 한 턴 순수 로직: A 입력 조립, 추출 결과의 저장·진행 계획, B 입력 조립(표 찾기).

판단은 모델(LoRA-A)과 turn-engine의 `validate_extraction`이 한다. 이 모듈은 저장소 문서와 표에서 정해진 것을 '고르고 옮길' 뿐이다:
- A 입력 `state`: 지금 문항의 저장 값 -> 직전 문항(건너뛴 문항은 뺀다)과 참조 원천 문항의 저장 값 -> 지금 문항의 MISSING·UNCLEAR 순서(학습 데이터 규칙,
  표본에서 읽은 것이라 미확정). 한 항목은 `{"value", "status"}` 순서.
- 저장: `save=true`인 값만. 같은 칸에 값이 있으면 이력에 쌓는 정정(NEW_REVISION). CONFLICT 등 save=false 값은 저장하지 않는다.
- B 입력: 모든 수준의 키를 정렬해(학습 데이터 형태) 만든다. 직렬화는 model-runtime의 몫이다.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .assembly import Bank
from .flow import Flow
from .response_tables import ResponseTables, item_of, slot_name
from .store import effects

SKIP_REASON = {"REQUEST_SKIP": "REQUEST_SKIP", "REFUSED": "REFUSED"}   # SKIP 사유 -> 건너뜀 사유. HOLD는 UNCERTAIN
SAME_ACTIONS = ("REASK", "CONFIRM", "REDIRECT")


class TurnInputError(Exception):
    """입력을 조립할 수 없다(표·문항 세트에 없음). code는 값 없는 이름."""
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Violation:
    """서비스 쪽 검사의 위반(turn_engine.Violation과 같은 모양, 값 없음)."""
    code: str
    path: str
    message: str


# ---------------------------------------------------------------- A 입력

def last_response_text(doc: Mapping) -> Optional[str]:
    """직전 응답 전체 문장(A 입력의 current_question). 대화 로그의 마지막 응답이다. 없으면 None."""
    for e in reversed(doc["dialogue_log"]):
        if e.get("response_text"):
            return e["response_text"]
    return None


def _saved_by_slot(answers: Mapping[str, tuple]) -> dict:
    """건너뛰기 표가 보는 형태: 칸 이름(종류 괄호 없음) -> 저장된 값."""
    return {slot_name(k): v[0] for k, v in answers.items()}


def build_state(doc: Mapping, flow: Flow) -> dict:
    """A 입력의 state(삽입 순서가 규칙이다, 위 설명)."""
    prog = doc["progress_record"]
    q = prog["current_question_id"]
    answers = effects.answers_by_key(doc["answer_records"])
    state: dict[str, dict] = {}

    def add_item(item: str) -> None:
        for k in sorted((k for k in answers if item_of(k) == item), key=flow.sort_key):
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
        if item_of(o["key"]) == q:
            state.setdefault(o["key"], {"value": None, "status": o["status"]})
    return state


def build_extract_input(doc: Mapping, bank: Bank, flow: Flow, utterance: str) -> dict:
    """ActionExtractionInput. 최상위 키는 알파벳 순(학습 데이터와 같다)."""
    q = doc["progress_record"]["current_question_id"]
    cq = last_response_text(doc)
    if cq is None:                      # 직전 응답이 없다(응답 실패 등): 문항 은행의 질문 문장
        cq = bank.sentence(q)
        if cq is None:
            raise TurnInputError("NO_QUESTION_SENTENCE")
    return {"current_question": cq, "question_id": q, "state": build_state(doc, flow), "user_utterance": utterance}


# ---------------------------------------------------------------- 추출 결과 -> 저장·진행

def check_slots(output: Mapping, records: list) -> list[Violation]:
    """저장하는 값(save=true)의 칸이 문항 세트의 슬롯에 있는가. 없으면 지어내지 않고 위반으로 본다(재시도 대상)."""
    have = {effects.slot_key(r["slot"]) for r in records}
    out = []
    for i, v in enumerate(output["values"]):
        if v["save"] and effects.slot_key(effects.slot_ref(v["key"])) not in have:
            out.append(Violation("UNKNOWN_SLOT", f"values/{i}/key", "저장하려는 칸이 문항 세트에 없다"))
    return out


def plan_writes(doc: Mapping, output: Mapping) -> dict:
    """TurnWriteSet. 입력 문서는 바꾸지 않는다."""
    answers = effects.answers_by_key(doc["answer_records"])
    updates, saved_keys = [], set()
    for v in output["values"]:
        if not v["save"]:
            continue
        saved_keys.add(v["key"])
        cur = answers.get(v["key"])
        if cur is not None and cur[0] == v["value"] and isinstance(cur[0], bool) == isinstance(v["value"], bool) and cur[1] == v["precision"]:
            continue                      # 같은 값을 다시 말했다: 바꿀 것이 없다
        updates.append({"slot": effects.slot_ref(v["key"]), "action": "FIRST_WRITE" if cur is None else "NEW_REVISION",
                        "value": copy.deepcopy(v["value"]), "precision": v["precision"]})
    nxt = output["next_item"]
    prog = doc["progress_record"]
    open_slots = {o["key"]: o["status"] for o in prog["open_slots"]}
    for k in saved_keys:
        open_slots.pop(k, None)
    for v in output["values"]:
        if not v["save"] and v["status"] in ("MISSING", "UNCLEAR"):
            open_slots[v["key"]] = v["status"]
    open_list = [{"key": k, "status": s} for k, s in open_slots.items() if nxt != "END" and item_of(k) == nxt]
    skipped_add, skipped_remove = [], sorted({item_of(k) for k in saved_keys if any(e["question_id"] == item_of(k) for e in prog["skipped"])})
    if output["action"] in ("SKIP", "HOLD"):
        reason = "UNCERTAIN" if output["action"] == "HOLD" else SKIP_REASON[output["reason"]]
        for item in dict.fromkeys(item_of(t) for t in output.get("targets", [])):
            skipped_add.append({"question_id": item, "reason": reason})
            if item in skipped_remove:
                skipped_remove.remove(item)
    update = {"current_question_id": nxt, "open_slots": open_list, "skipped_add": skipped_add, "skipped_remove": skipped_remove,
              "session_status": "ENDED_COMPLETE" if nxt == "END" else None, "pause_history_add": []}
    return {"answer_updates": updates, "progress_record_update": update}


# ---------------------------------------------------------------- B 입력 (표 찾기)

def sorted_deep(x: Any) -> Any:
    """모든 수준의 키를 정렬한다(B 입력의 학습 데이터 형태)."""
    if isinstance(x, dict):
        return {k: sorted_deep(x[k]) for k in sorted(x)}
    if isinstance(x, list):
        return [sorted_deep(v) for v in x]
    return x


def _next_text(item: str, bank: Bank, tables: ResponseTables, filled: Mapping[str, Any], partial_ok: bool) -> str:
    text = bank.sentence(item)
    if text is None:
        raise TurnInputError("NO_QUESTION_SENTENCE")
    if partial_ok and any(slot_name(k).startswith(item + ".") for k in filled):
        rem = tables.remaining_text(item, {slot_name(k): v for k, v in filled.items()})
        if rem:
            return rem            # 57행 문제: 이미 저장된 칸은 다시 묻지 않고 남은 칸만 묻는다
    return text


def build_start_request(bank: Bank, tables: ResponseTables, first_item: str) -> dict:
    """세션 시작: A를 거치지 않는 ResponseRequest(action=START)."""
    text = bank.sentence(first_item)
    if text is None:
        raise TurnInputError("NO_QUESTION_SENTENCE")
    return sorted_deep({"action": "START", "topic": tables.topic(first_item),
                        "next": {"type": "NEXT", "question_id": first_item, "text": text}})


def build_response_request(out: Mapping, extract_input: Mapping, doc_after: Mapping, bank: Bank, tables: ResponseTables) -> dict:
    """ResponseRequest(START 외). 표 찾기만 한다. 문장 필드(topic, known, ask, previous, current, hold)는 데이터셋 담당의 규칙을 옮긴
    `ResponseTables.b_fields`가 만들고, 여기서는 `next`만 붙인다(문항 은행의 질문 문장, 57행 문제는 남은 칸 문장)."""
    nxt = out["next_item"]
    req: dict[str, Any] = tables.b_fields(out, extract_input["question_id"], extract_input["user_utterance"], extract_input["state"])
    if nxt == "END":
        req["next"] = {"type": "END"}
    elif out["action"] in SAME_ACTIONS:
        req["next"] = {"type": "SAME", "question_id": nxt, "text": _next_text(nxt, bank, tables, {}, False)}
    else:
        filled = {k: v[0] for k, v in effects.answers_by_key(doc_after["answer_records"]).items() if item_of(k) == nxt}
        req["next"] = {"type": "NEXT", "question_id": nxt, "text": _next_text(nxt, bank, tables, filled, True)}
    return sorted_deep(req)
