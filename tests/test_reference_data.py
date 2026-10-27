"""선택 시험: 데이터셋 담당의 학습 데이터(C1)와 서비스 규칙의 대조. SURVEY_REFERENCE_DATA_DIR가 없으면 건너뛴다(데이터는 리포에 없다).
전체 대조와 결과 표는 scripts/reference_agreement.py. 여기서는 각 split 앞 300행으로 빠르게 확인한다."""
import os
from pathlib import Path

import pytest

D = os.environ.get("SURVEY_REFERENCE_DATA_DIR", "")
pytestmark = pytest.mark.skipif(not D or not (Path(D) / "lora_a_val.jsonl").exists(), reason="SURVEY_REFERENCE_DATA_DIR 없음: 학습 데이터 대조 건너뜀")


@pytest.fixture(scope="module")
def parts():
    import survey_schema
    from survey_service.assembly import Bank
    from survey_service.flow import load_flow
    from survey_service.response_tables import ResponseTables
    from .sample_bank import sample_bank
    return Path(D), load_flow(), Bank(sample_bank()), ResponseTables(survey_schema.load_data("response_tables"))


def _ra():
    import scripts.reference_agreement as ra
    return ra


def test_b_input_matches_training_data(parts):
    d, flow, bank, tables = parts
    tot, bad = _ra().b_agreement(d, ["val", "test"], tables, 300)
    assert tot["불일치"] == 0 and tot["불일치(START)"] == 0 and not any(k.startswith("짝") for k in tot) and tot["일치"] > 400


def test_correct_training_answers_are_not_rejected_by_value_validation(parts):
    d, flow, bank, tables = parts
    tot, codes, ex = _ra().validation_check(d, ["train", "val", "test"], bank, flow, 400)
    assert tot["거부"] == 0, (dict(codes), dict(ex))
    assert tot["통과"] == tot["A 정답 행(전체)"] >= 1000


def test_a_state_order_agreement_stays_high(parts):
    d, flow, bank, tables = parts
    tot, ex = _ra().a_state_agreement(d, ["val", "test"], bank, flow, 600)
    assert tot["일치(순서·값 모두)"] / tot["A 행(전체)"] >= 0.95
    assert not [k for k in tot if k.startswith("불일치") and "순서만" not in k]            # 다르더라도 칸 집합·값은 같다(순서 차이뿐)
