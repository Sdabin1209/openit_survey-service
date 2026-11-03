"""U80: A 입력 state의 키 순서는 학습 데이터 규칙이고(지금 문항 저장값 -> 직전·참조 문항 저장값 -> 지금 문항의 빈 칸), 서비스가 그 순서를 직렬화로 흐트러뜨리지 않는다.

왜: 모델은 state의 순서를 본다(학습 데이터와 같은 글자여야 한다). 순서는 dict 삽입 순서이므로 어디선가 sort_keys=True로 직렬화하면 조용히 사라진다.
"""
import ast
import json
from pathlib import Path

import survey_service
from survey_service.components import extract_request
from survey_service.components import session_store as effects
from survey_service.components.flow_table import load_flow

from .sample_bank import sample_bank

FLOW = load_flow()


def _doc():
    doc = effects.initial_state("S", sample_bank(), "Q4_1")
    def put(k, v):
        doc["answer_records"] = effects.apply_answer_updates(doc["answer_records"], [{"slot": effects.slot_ref(k), "action": "FIRST_WRITE", "value": v, "precision": "exact"}])
    for k, v in [("Q4.answer", True), ("Q4_1.total_years", 5), ("Q4_1.status", "current")]:
        put(k, v)
    doc["progress_record"]["open_slots"] = [{"key": "Q4_1.daily_count", "status": "UNCLEAR"}]
    return doc


def test_state_puts_current_item_saved_values_first_then_previous_and_reference_then_open_slots():
    st = extract_request.build_state(_doc(), FLOW)
    # Q4_1: 지금 문항의 저장값(문항 안은 문진 스키마의 칸 순서) -> 직전 문항 Q4 -> 지금 문항의 미저장 칸
    assert list(st) == ["Q4_1.status", "Q4_1.total_years", "Q4.answer", "Q4_1.daily_count"]


def test_state_order_survives_the_json_round_trip_the_wire_uses():
    st = extract_request.build_state(_doc(), FLOW)
    assert list(json.loads(json.dumps(st, ensure_ascii=False))) == list(st)       # sort_keys 없는 직렬화는 삽입 순서를 지킨다


def test_service_code_never_serializes_with_sort_keys():
    """정적 시험: survey_service 안의 어떤 호출도 sort_keys 키워드를 쓰지 않는다(쓰면 A 입력 state의 순서가 사전순으로 바뀐다)."""
    root = Path(survey_service.__file__).parent
    hits = []
    for p in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"), filename=str(p))):
            if isinstance(node, ast.keyword) and node.arg == "sort_keys":
                hits.append(f"{p.relative_to(root)}:{node.value.lineno if hasattr(node.value, 'lineno') else '?'}")
    assert hits == []
