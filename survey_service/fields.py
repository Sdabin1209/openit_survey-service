"""문항 은행의 칸 정의(SlotDef)에서 계약 QuestionFieldSpec을 만든다 (U32, 계약 2.4.0, ADR-0033).

판단은 하지 않는다: 칸에 적힌 자료형·단위·범위와 선택지(choices), 문항의 반복 키를 계약 모양으로 옮길 뿐이다. 값의 정본은 문항 은행(작성은 question-authoring)이고
학습 데이터셋 question_fields와 같아야 한다(시험이 52칸으로 대조한다). I9 extract 요청 `ExtractRequest.fields`로 추론 서버에 싣고, 서버가 값 규칙을 보증한다(R7).
"""
from __future__ import annotations

from typing import Mapping, Optional

_COPY = ("unit", "minimum", "maximum", "exclusive_minimum")


def build_field_specs(bank: Mapping) -> Optional[dict]:
    """QuestionFieldSpec(`{"fields": [...]}`). 자료형(data_type)이 적힌 칸이 하나도 없으면 None(규격을 싣지 않는다)."""
    fields = []
    for q in bank["questions"]:
        for s in q["slots"]:
            if "data_type" not in s:
                continue
            f: dict = {"slot": f"{q['question_id']}.{s['slot_id']}", "data_type": s["data_type"]}
            for k in _COPY:
                if k in s:
                    f[k] = s[k]
            if s["data_type"] == "option" and s.get("choices"):
                f["options"] = list(s["choices"])               # 지금 문항 은행의 choices를 그대로 쓴다
            if q.get("repeat_keys"):
                f["repeat_keys"] = list(q["repeat_keys"])
            fields.append(f)
    return {"fields": fields} if fields else None


def missing_field_specs(bank: Mapping) -> list[str]:
    """칸 규격이 없는 칸의 이름(`문항.칸`). 규격이 없는 칸은 서버가 값 규칙을 보증하지 못하므로 문항 은행 검증이 적재를 거부한다(계약 3.0.0: extract는 항상 fields와 함께, ADR-0043).
    규격이 없다는 것은 `data_type`이 없거나, option 칸인데 선택지(`choices`)가 비어 있는 것이다(선택지를 모르면 서버가 선택지 값을 막을 수 없다)."""
    out = []
    for q in bank["questions"]:
        for s in q["slots"]:
            if "data_type" not in s or (s["data_type"] == "option" and not s.get("choices")):
                out.append(f"{q['question_id']}.{s['slot_id']}")
    return out
