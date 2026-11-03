"""문항 순서·건너뛰기 표(D76). 문진표(QuestionBank)에서 만든다(ADR-0057): 고정 흐름 파일은 없다.

순서는 문항 `order`, 건너뛰기는 `prerequisite`의 부정, 참조 원천은 `affected_followups`의 역방향, 칸 순서는 문항 `slots` 배열 순서다.
서비스는 이 표로 A 입력의 `state`를 만들고, I9 extract 요청의 `flow`(계약 QuestionFlowTable: order, skips)로 서버에 싣는다.
출력의 의미 검증은 서버가 보증하므로(ADR 0010, R7) 서비스는 이 표로 출력을 검사하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# 건너뛰기 조건은 prerequisite(참이면 묻는다)의 부정이다. 왜 이 쌍인가: not(a == b) = (a != b), not(a > b) = (a <= b), not(a < b) = (a >= b)
_NEGATE = {"==": "!=", "!=": "==", ">": "<=", "<=": ">", "<": ">=", ">=": "<"}


def _same(a: Any, b: Any) -> bool:
    """칸 값과 규칙 값이 같은가. 불리언과 숫자는 다른 값이다(False != 0)."""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def holds(saved: Any, op: str, value: Any) -> bool:
    """`saved op value`. 같음·다름은 bool과 수를 구분한다. 크기 비교는 둘 다 수이거나 둘 다 문자열일 때만 성립한다(그 밖은 거짓: 비교할 수 없는 값으로 문항을 건너뛰지 않는다)."""
    if op == "==":
        return _same(saved, value)
    if op == "!=":
        return not _same(saved, value)
    if not ((_num(saved) and _num(value)) or (isinstance(saved, str) and isinstance(value, str))):
        return False
    return {">": saved > value, ">=": saved >= value, "<": saved < value, "<=": saved <= value}[op]


@dataclass(frozen=True)
class FlowTable:
    """문진표에서 만든 order·skips(계약 QuestionFlowTable과 같은 모양). 조건 연산자는 6개(`==` `!=` `>` `>=` `<` `<=`)다."""
    order: tuple
    skips: tuple = ()

    def index(self, item: str) -> int | None:
        return self.order.index(item) if item in self.order else None

    def is_skipped(self, item: str, saved: Mapping[str, Any]) -> bool:
        return any(r["skip_item"] == item and r["slot"] in saved and holds(saved[r["slot"]], r["op"], r["value"]) for r in self.skips)

    def next_after(self, item: str, saved: Mapping[str, Any]) -> str | None:
        """`item` 다음에 물을 문항(건너뛰기 표로 빠지는 문항은 뺀다). 마지막이면 "END". 순서표에 없는 문항이면 None(다음을 알 수 없다)."""
        i = self.index(item)
        if i is None:
            return None
        for j in range(i + 1, len(self.order)):
            if not self.is_skipped(self.order[j], saved):
                return self.order[j]
        return "END"

    def wire(self) -> dict:
        """계약 QuestionFlowTable(JSON)."""
        return {"order": list(self.order), "skips": [dict(r) for r in self.skips]}


@dataclass(frozen=True)
class Flow:
    table: FlowTable
    reference_sources: Mapping[str, tuple]
    slot_order: Mapping[str, tuple]
    flow_id: str
    raw: Mapping | None = None      # 만든 원본(QuestionBank 문서). wiring이 입력 스키마 검증에 쓴다

    def previous_item(self, item: str, saved: Mapping[str, Any]) -> str | None:
        """`item` 바로 앞에 물은 문항(건너뛴 문항은 뺀다). 첫 문항이면 None."""
        i = self.table.index(item)
        if i is None:
            return None
        for j in range(i - 1, -1, -1):
            if not self.table.is_skipped(self.table.order[j], saved):
                return self.table.order[j]
        return None

    def sort_key(self, key: str):
        """한 문항 안의 칸 순서: 종류(대괄호) -> 문진 스키마의 slot_order(없으면 칸 이름 정렬)."""
        head, slot = key.rsplit(".", 1)
        item, _, rk = head.partition("[")
        order = self.slot_order.get(item)
        idx = order.index(slot) if order and slot in order else len(order or ())
        return (rk, idx, slot)


def _slot_key(ref: Mapping) -> str:
    """SlotRef -> 모델 키. 반복 키가 있으면 `문항[키].칸`."""
    rk = ref.get("repeat_key") or ""
    return f"{ref['question_id']}[{rk}].{ref['slot_id']}" if rk else f"{ref['question_id']}.{ref['slot_id']}"


def build_flow(bank: Mapping) -> Flow:
    """적재된 문진표(QuestionBank)에서 Flow를 만든다. 문항 order가 겹치면 ValueError(순서를 정할 수 없다)."""
    qs = sorted(bank["questions"], key=lambda q: q["order"])
    orders = [q["order"] for q in qs]
    if len(set(orders)) != len(orders):
        raise ValueError("문항 order가 겹친다")
    ids = [q["question_id"] for q in qs]
    if len(set(ids)) != len(ids):
        raise ValueError("문항 ID가 겹친다")
    skips = []
    for q in qs:
        pre = q.get("prerequisite")
        if pre:
            skips.append({"slot": _slot_key(pre["slot"]), "op": _NEGATE[pre["op"]], "value": pre["value"], "skip_item": q["question_id"]})
    known = set(ids)
    refs: dict[str, list] = {}
    for q in qs:                                                 # A의 affected_followups에 B가 있으면 B의 참조 원천에 A(문항 order 순)
        for b in q["affected_followups"]:
            if b not in known:
                raise ValueError(f"affected_followups가 없는 문항을 가리킨다: {b}")
            refs.setdefault(b, []).append(q["question_id"])
    table = FlowTable(order=tuple(ids), skips=tuple(skips))
    return Flow(table, {k: tuple(v) for k, v in refs.items()}, {q["question_id"]: tuple(s["slot_id"] for s in q["slots"]) for q in qs},
                bank["question_set_id"], bank)


def flow_problems(bank: Mapping) -> list:
    """적재 전 검사: 흐름을 만들 수 없는 문진표의 사유 목록(비면 만들 수 있다). 건너뛰기 조건이 가리키는 칸이 문진표에 있는지도 본다."""
    try:
        flow = build_flow(bank)
    except ValueError as e:
        return [str(e)]
    slots = {(q["question_id"], s["slot_id"]) for q in bank["questions"] for s in q["slots"]}
    rk = {q["question_id"]: set(q["repeat_keys"]) for q in bank["questions"]}
    out = []
    for q in bank["questions"]:
        pre = q.get("prerequisite")
        if not pre:
            continue
        ref = pre["slot"]
        if (ref["question_id"], ref["slot_id"]) not in slots:
            out.append(f"prerequisite가 없는 칸을 가리킨다: {q['question_id']} -> {_slot_key(ref)}")
        elif ref.get("repeat_key") and ref["repeat_key"] not in rk[ref["question_id"]]:
            out.append(f"prerequisite의 repeat_key가 문항의 repeat_keys에 없다: {q['question_id']} -> {_slot_key(ref)}")
        elif flow.table.index(ref["question_id"]) is not None and flow.table.index(ref["question_id"]) >= flow.table.index(q["question_id"]):
            out.append(f"prerequisite가 자기보다 앞서 묻지 않는 문항을 가리킨다: {q['question_id']} -> {_slot_key(ref)}")
    return out
