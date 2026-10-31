"""2차 점검 H1·H2: 임시 위험 목록의 오탐(부정·일상 표현)과 미탐 고정 시험. 목록은 임상 검토 전 임시다."""
import ast
from pathlib import Path

import pytest

from survey_service.components.risk_screen import RuleRiskSource

NEGATIVE = [
    "자살 생각은 없어요", "가슴이 아프지 않아요", "기절한 적 없어요", "마비는 없어요", "응급실에 간 적 없어요", "혈압이 119에 80이에요", "당뇨약 메트포르민 1190mg 먹어요",
    "건강하게 살려고 운동해요", "오래 살려면 뭐 해야 하나요", "손목을 그냥 삐었어요", "살기 싫은 음식은 없어요", "피가 많이 나진 않아요", "피를 토한 적은 없어요",
    "숨이 차서 말하기 힘들진 않아요", "심장이 아프진 않아요", "죽고싶진 않아요", "죽고 싶지 않아요", "어지러워서 쓰러질 것 같지는 않아요", "한쪽이 저리지는 않아요",
    "어머니가 뇌졸중으로 마비가 왔었어요", "몇 년 전에 중풍으로 한 번 쓰러졌었어요", "I have no chest pain", "no suicide thoughts", "그냥 말이 안 나와요", "요즘 정신이 없어요",
    "아니요, 숨쉬기 힘들지 않아요", "자해한 적 없습니다",
]
POSITIVE = [
    "죽고 싶고 아무 희망이 없어요", "가슴이 아파요 약을 먹어도 안 나아요", "자살 생각이 계속 나요 없어지지 않아요", "가슴이 아파요 아니 정말 많이 아파요",
    "가족이 없어서 죽고 싶어요", "남편이 때려서 죽을 것 같아요", "엄마 때문에 자살하고 싶어요", "3개월 전부터 가슴이 아파요", "한 달 전부터 숨쉬기 힘들어요",
    "반신마비가 왔어요", "전신마비 같아요", "너무죽고싶어요", "극심한가슴통증", "심한호흡곤란이에요",
    "살고 싶지 않아요", "죽을 것 같아요", "죽고 싶어요", "죽고싶어요", "뛰어내리고 싶어요", "숨쉬기 너무 힘들어요", "숨이 안 쉬어져요", "숨을 못 쉬겠어요", "숨이안쉬어져요",
    "가슴에 통증이 심해요", "가슴이 터질 것 같아요", "갑자기 쓰러졌어요", "의식을 잃었어요", "지금 가슴이 아파요", "손목을 그었어요", "손목을 긋고 싶어요",
    "I want to kill myself", "suicide", "I can't breathe", "chest pain", "I have chest pain", "119 불러 주세요", "119에 전화해 주세요", "살려 주세요", "살려줘요",
    "가슴이 아파요 하지만 괜찮아요", "한쪽 팔이 마비된 것 같아요", "말이 어눌해졌어요", "피가 계속 나요", "더 이상 살 수 없어요", "살 의미가 없어요",
]


@pytest.fixture(scope="module")
def src():
    return RuleRiskSource.provisional_default()


@pytest.mark.parametrize("text", NEGATIVE)
def test_normal_answers_do_not_stop_the_session(src, text):
    assert src.match(text) is None, text


@pytest.mark.parametrize("text", POSITIVE)
def test_red_flags_are_caught(src, text):
    assert src.match(text) is not None, text


def test_counters_show_what_was_excluded_and_describe_stays_provisional():
    s = RuleRiskSource.provisional_default()
    for t in ("자살 생각은 없어요", "어머니가 뇌졸중으로 마비가 왔었어요", "몇 년 전에 한 번 쓰러졌었어요"):
        assert s.match(t) is None
    d = s.describe()
    assert d["negated_hits"] == 1 and d["third_party_hits"] == 1 and d["history_hits"] == 1 and d["provisional"] is True


def test_match_is_linear_in_the_number_of_hits():
    s = RuleRiskSource.provisional_default()
    text = "가슴이 아파요. " * 8000
    assert s.match(text) is not None      # 결과만 본다(속도는 정확성이 아니다: 측정은 시험 묶음 밖)


def test_phrases_do_not_merge_across_words():
    s = RuleRiskSource.provisional_default()
    assert s.match("손목을 그냥 삐었어요") is None and s.match("손목을그었어요") is not None            # 구절 안 공백은 자유, 구절 자체가 완결된 어간이라 "그냥"은 걸리지 않는다


def test_provisional_structure_is_stable_for_the_e2e_ast_reader():
    """deploy E2E(5k·5m)가 ast로 읽는다: `_PROVISIONAL` = {규칙 키: 문자열 튜플} 리터럴, "chest-pain"의 첫 구절이 그대로 적중한다."""
    tree = ast.parse(Path(__file__).resolve().parent.parent.joinpath("survey_service/components/risk_screen.py").read_text(encoding="utf-8"))
    d = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_PROVISIONAL")
    assert all(isinstance(v, tuple) and v and all(isinstance(x, str) for x in v) for v in d.values()) and "chest-pain" in d
    assert RuleRiskSource.provisional_default().match(d["chest-pain"][0]) is not None
    assert RuleRiskSource.provisional_default().match(d["chest-pain"][0] + "요") is not None
