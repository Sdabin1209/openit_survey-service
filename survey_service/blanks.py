"""빈칸의 원인(U35, ADR-0036): 세션 문서에서 '값이 저장되지 않은 칸'과 그 원인 코드를 뽑는다. 판단하지 않고 A 출력이 이미 정한 status를 옮긴다.

원인 코드와 A 출력의 대응(학습 데이터셋 labels의 정본과 맞춘다):
- UNKNOWN  = status UNKNOWN (HOLD / reason UNCERTAIN: 환자가 모른다고 함)
- REJECTED = status REJECTED (SKIP / reason REFUSED·REQUEST_SKIP: 거절하거나 나중에 하겠다고 함)
- UNCLEAR  = status UNCLEAR (REASK / OUT_OF_RANGE, UNKNOWN_UNIT, VAGUE_QUANTITY, OTHER_BEVERAGE, RANGE_SPANS_OPTIONS: 불분명하거나 범위 밖이라 값이 정해지지 않음)
status MISSING(아직 못 받음)과 CONFLICT(확인 중)은 원인이 정해진 빈칸이 아니므로 올리지 않는다. 환자 발화 원문은 쓰지 않는다(감사 C2).
"""
from __future__ import annotations

from typing import Mapping

from .response_tables import item_of

CAUSE_OF_STATUS = {"UNKNOWN": "UNKNOWN", "REJECTED": "REJECTED", "UNCLEAR": "UNCLEAR"}
CAUSE_OF_SKIP_REASON = {"UNCERTAIN": "UNKNOWN", "REFUSED": "REJECTED", "REQUEST_SKIP": "REJECTED"}      # 칸 정보가 없는 건너뛴 문항의 대응


def blank_details(doc: Mapping) -> list[dict]:
    """`[{item, slot?, reason}]`. 대화 로그를 순서대로 읽어 칸마다 마지막 원인을 남기고, 나중에 값이 저장된 칸은 뺀다.
    진행 기록의 건너뛴 문항 중 로그에서 칸을 찾지 못한 것은 문항만 적는다(slot 생략)."""
    blanks: dict[str, str] = {}
    for e in doc["dialogue_log"]:
        ex = e.get("extract_output")
        if e["turn_result"] != "COMMITTED" or not ex:
            continue
        for v in ex["values"]:
            key = v["key"]
            if v["save"]:
                blanks.pop(key, None)
            elif v["status"] in CAUSE_OF_STATUS:
                blanks.pop(key, None)
                blanks[key] = CAUSE_OF_STATUS[v["status"]]
    out = [{"item": item_of(k), "slot": k, "reason": r} for k, r in blanks.items()]
    covered = {d["item"] for d in out}
    for s in doc["progress_record"]["skipped"]:
        if s["question_id"] not in covered:
            out.append({"item": s["question_id"], "reason": CAUSE_OF_SKIP_REASON[s["reason"]]})
    return out
