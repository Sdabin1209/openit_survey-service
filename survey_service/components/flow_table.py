"""문항 순서·건너뛰기 표(D76). 소유는 survey-service이고 값은 문진 스키마(`schema/data/question_flow.json`, 계약 QuestionFlow)에 있다.

서비스는 문진 스키마에서 표를 만들어 A 입력의 `state`를 만드는 데 쓰고, I9 extract 요청의 `flow`(계약 QuestionFlowTable: order, skips)로 서버에 싣는다.
출력의 의미 검증은 서버가 보증하므로(ADR 0010, R7) 서비스는 이 표로 출력을 검사하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import jsonschema
import survey_schema


def _same(a: Any, b: Any) -> bool:
    """칸 값과 규칙 값이 같은가. 불리언과 숫자는 다른 값이다(False != 0)."""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


@dataclass(frozen=True)
class FlowTable:
    """문진 스키마 QuestionFlow의 order·skips(계약 QuestionFlowTable과 같은 모양). 조건은 `==`만 쓴다(문진 스키마가 정한다)."""
    order: tuple
    skips: tuple = ()

    def index(self, item: str) -> int | None:
        return self.order.index(item) if item in self.order else None

    def is_skipped(self, item: str, saved: Mapping[str, Any]) -> bool:
        return any(r["skip_item"] == item and r["slot"] in saved and _same(saved[r["slot"]], r["value"]) for r in self.skips)

    def wire(self) -> dict:
        """계약 QuestionFlowTable(JSON)."""
        return {"order": list(self.order), "skips": [dict(r) for r in self.skips]}


@dataclass(frozen=True)
class Flow:
    table: FlowTable
    reference_sources: Mapping[str, tuple]
    slot_order: Mapping[str, tuple]
    flow_id: str
    raw: Any = None          # 만든 원본 문서(QuestionFlow). 호출 전 입력 검증이 이 문서를 가리킨다(wiring/checked.py)
    raw: Mapping | None = None      # 만든 원본(QuestionFlow 문서). wiring이 입력 스키마 검증에 쓴다

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


def build_flow(data: Mapping) -> Flow:
    if len(set(data["order"])) != len(data["order"]) or any(s["skip_item"] not in data["order"] for s in data["skips"]):
        raise ValueError("문항 순서에 중복이 있거나 건너뛸 문항이 순서에 없다")
    table = FlowTable(order=tuple(data["order"]), skips=tuple({k: s[k] for k in ("slot", "op", "value", "skip_item")} for s in data["skips"]))
    return Flow(table, {k: tuple(v) for k, v in data["reference_sources"].items()},
                {k: tuple(v) for k, v in data["slot_order"].items()}, data["flow_id"], data)


def load_flow(version: int = survey_schema.DEFAULT_VERSION) -> Flow:
    """문진 스키마에서 표를 읽고 계약(QuestionFlow)으로 검증한 뒤 만든다."""
    data = survey_schema.load_data("question_flow", version)
    jsonschema.validate(data, survey_schema.load_schema("QuestionFlow", version))
    return build_flow(data)
