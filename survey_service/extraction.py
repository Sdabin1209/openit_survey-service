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


_MAX_NUM = 1_000_000          # 값 타당성의 거친 상한(사람이 답하는 양·횟수·나이·일수). 넘으면 지어낸 값으로 본다 (ADR-0017)
_MAX_STR = 200


def _family(item: str) -> str:
    return item.split("_", 1)[0].split(".", 1)[0]


def _slot_def(bank: Bank, ref: Mapping) -> Optional[Mapping]:
    q = bank.by_id.get(ref["question_id"])
    return next((x for x in (q or {}).get("slots", []) if x["slot_id"] == ref["slot_id"]), None)


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) <= _MAX_NUM


def check_values(output: Mapping, doc: Mapping, bank: Optional[Bank], flow: Optional[Flow] = None) -> list[Violation]:
    """turn-engine의 validate_extraction 뒤에 서비스가 한 번 더 보는 값 타당성(감사 TE-1, D88): 위반이면 값을 저장하지 않고 같은 요청을 1회 재시도, 그래도면 턴 NOT_COMMITTED.
    (1) 저장하는 값의 칸이 현재 문항이어야 한다(정정은 예외: status CORRECTION 또는 action CORRECT). (2) 값이 Value 형식이고 합리적인 크기다.
    (3) 슬롯 정의가 아는 만큼 타입이 맞는다: choices는 그 목록의 문자열, yes_no_map은 불리언(또는 yes/no 문자열), units는 단위가 목록에 있는 양 또는 숫자.
    (4) 이미 저장된 값이 있으면 같은 종류(불리언/숫자/문자열/양)여야 한다. 한계: QuestionBank에 칸의 값 타입 필드가 없어 처음 쓰는 자유 칸의 타입은 알 수 없다(CR-017)."""
    out: list[Violation] = []
    if bank is None:
        return out
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
    answers = effects.answers_by_key(doc["answer_records"])
    is_corr_action = output.get("action") == "CORRECT"
    for i, v in enumerate(output["values"]):
        if not v["save"]:
            continue
        path = f"values/{i}"
        ref = effects.slot_ref(v["key"])
        if _family(ref["question_id"]) not in fams and ref["question_id"] not in span and not (is_corr_action or v["status"] == "CORRECTION"):
            out.append(Violation("OUT_OF_CURRENT_ITEM", f"{path}/key", "현재 문항이 아닌 칸을 정정이 아닌데 저장하려 한다"))
            continue
        val = v["value"]
        slot = _slot_def(bank, ref)
        kind = ("bool" if isinstance(val, bool) else "num" if _is_num(val) else "amount" if isinstance(val, dict) and "amount" in val and "unit" in val
                else "str" if isinstance(val, str) and 0 < len(val) <= _MAX_STR else "coded" if isinstance(val, dict) and "amount" not in val and val else None)
        if kind is None:
            out.append(Violation("VALUE_INVALID", f"{path}/value", "값 형식이 아니거나 비합리적이다(숫자 상한 초과, 빈 문자열, 너무 긴 문자열)"))
            continue
        if kind == "amount" and (not _is_num(val["amount"]) or not isinstance(val["unit"], str)):
            out.append(Violation("VALUE_INVALID", f"{path}/value", "양 값의 amount·unit이 올바르지 않다"))
            continue
        if slot is not None:
            choices, units = slot.get("choices") or [], slot.get("units") or []
            catalog = {c.get("id") for c in slot.get("choice_catalog") or [] if isinstance(c, dict)}
            if choices and not (kind == "str" and (val in choices or val in catalog)) and kind != "coded":
                out.append(Violation("VALUE_TYPE_MISMATCH", f"{path}/value", "선택지 칸에 선택지 밖의 값이다"))
            elif slot.get("yes_no_map") and kind not in ("bool", "str"):
                out.append(Violation("VALUE_TYPE_MISMATCH", f"{path}/value", "예/아니오 칸에 맞지 않는 값이다"))
            elif units and kind == "amount" and val["unit"] not in {u.get("unit") for u in units if isinstance(u, dict)} | {u.get("unit_code") for u in units if isinstance(u, dict)}:
                out.append(Violation("VALUE_TYPE_MISMATCH", f"{path}/value", "단위가 칸의 단위 목록에 없다"))
        prev = answers.get(v["key"])
        if prev is not None and prev[0] is not None:
            pk = ("bool" if isinstance(prev[0], bool) else "num" if _is_num(prev[0]) else "amount" if isinstance(prev[0], dict) and "amount" in prev[0]
                  else "str" if isinstance(prev[0], str) else "coded")
            if pk != kind and {pk, kind} != {"num", "amount"}:
                out.append(Violation("VALUE_TYPE_MISMATCH", f"{path}/value", "이미 저장된 값과 종류가 다르다"))
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
