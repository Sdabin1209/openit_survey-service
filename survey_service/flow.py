"""문항 순서·건너뛰기 표(D76). 소유는 survey-service이고 값은 문진 스키마(`schema/data/question_flow.json`, 계약 QuestionFlow)에 있다.

서비스는 문진 스키마에서 turn-engine의 `QuestionFlowTable`을 만들어 `validate_extraction`에 넘긴다. turn-engine의 `DATASET_V2_FLOW`는
시험용 기준일 뿐이다(`tests/test_flow.py`가 두 표가 같은지 본다). 이 모듈은 A 입력의 `state`를 만드는 데 필요한 순서 규칙도 가진다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import jsonschema
import survey_schema
from turn_engine import QuestionFlowTable, SkipRule


@dataclass(frozen=True)
class Flow:
    table: QuestionFlowTable
    reference_sources: Mapping[str, tuple]
    slot_order: Mapping[str, tuple]
    flow_id: str

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
    table = QuestionFlowTable(order=data["order"], skips=[SkipRule(s["slot"], s["value"], s["skip_item"]) for s in data["skips"]])
    return Flow(table, {k: tuple(v) for k, v in data["reference_sources"].items()},
                {k: tuple(v) for k, v in data["slot_order"].items()}, data["flow_id"])


def load_flow(version: int = survey_schema.DEFAULT_VERSION) -> Flow:
    """문진 스키마에서 표를 읽고 계약(QuestionFlow)으로 검증한 뒤 만든다."""
    data = survey_schema.load_data("question_flow", version)
    jsonschema.validate(data, survey_schema.load_schema("QuestionFlow", version))
    return build_flow(data)
