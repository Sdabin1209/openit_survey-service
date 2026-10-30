"""문항 은행의 칸 정의(SlotDef)에서 계약 QuestionFieldSpec을 만든다 (U32, 계약 2.4.0, ADR-0033).

판단은 하지 않는다: 칸에 적힌 자료형·단위·범위와 선택지(choices), 문항의 반복 키를 계약 모양으로 옮길 뿐이다. 값의 정본은 문항 은행(작성은 question-authoring)이고
학습 데이터셋 question_fields와 같아야 한다(시험이 52칸으로 대조한다). I9 extract 요청 `ExtractRequest.fields`로 추론 서버에 싣고, 서버가 값 규칙을 보증한다(R7).
"""
from __future__ import annotations

from typing import Mapping, Optional

_COPY = ("unit", "minimum", "maximum", "exclusive_minimum")


def option_codes(slot: Mapping) -> list[str]:
    """option 칸의 선택지 코드. 문진 스키마는 choice_catalog가 있으면 choices를 비우게 하므로(question-authoring은 choice_catalog로 낸다) 카탈로그의 code가 우선이고, 없으면 choices다."""
    cat = slot.get("choice_catalog")
    return [c["code"] for c in cat] if cat else list(slot.get("choices") or [])


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
            if s["data_type"] == "option" and option_codes(s):
                f["options"] = option_codes(s)
            if q.get("repeat_keys"):
                f["repeat_keys"] = list(q["repeat_keys"])
            fields.append(f)
    return {"fields": fields} if fields else None


def invalid_field_specs(bank: Mapping) -> list[str]:
    """계약 QuestionFieldSpec을 어기는 칸의 `문항.칸`과 이유. 문진 스키마(QuestionBank)가 표현하는 규칙(자료형 필수, option의 선택지 필수, id 문자, choices 중복)은 스키마가 먼저 거부하고,
    여기는 스키마로 표현할 수 없는 것(choice_catalog 코드 중복)과 두 스키마 사이의 어긋남을 적재 때 잡는다(ADR-0045). 그대로 두면 적재는 되고 extract가 400이 된다."""
    from boundary_contracts import validator
    specs = build_field_specs(bank)
    if specs is None:
        return ["(자료형이 적힌 칸이 하나도 없다)"]
    v = validator("QuestionFieldSpec")
    out = []
    for i, f in enumerate(specs["fields"]):
        for e in v.iter_errors({"fields": [f]}):
            out.append(f"{f['slot']}: " + (e.message if not list(e.path)[2:] else f"{list(e.path)[2]} {e.message}"))
    return out
