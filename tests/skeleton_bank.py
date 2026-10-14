"""골든 예시의 응답 기록 구성(슬롯 목록)만 복원한 문항 세트. 골든에는 문항 세트가 없다(CR-002)."""
from __future__ import annotations

# (question_id, name, slot_ids, repeat_keys)
_SHAPE = [
    ("Q1", "흡연", ["상태"], []),
    ("Q2", "흡연량", ["개비"], []),
    ("Q3a", "음주 여부", ["여부"], []),
    ("Q3b", "음주 상세", ["빈도", "종류", "1회양"], []),
    ("Q4", "과음 여부", ["여부"], []),
    ("Q5", "수면", ["시간"], []),
    ("Q6", "자살 생각", ["여부"], []),
    ("Q7", "혈압", ["수축기", "이완기"], []),
    ("Q8", "운동", ["횟수"], []),
    ("Q3c", "종류별 음주량", ["양"], ["소주", "맥주", "막걸리", "와인", "기타"]),
]


def skeleton_bank(version: int = 1) -> dict:
    questions = []
    for i, (qid, name, slots, rks) in enumerate(_SHAPE, start=1):
        questions.append({
            "question_id": qid, "name": name, "order": i, "prerequisite": None,
            "question_sentence": f"{name}을(를) 여쭙니다.", "refusal_explanation": "넘어가셔도 됩니다.",
            "guide_sentences": [], "affected_followups": [],
            "policy": {"question_limit": 3, "unknown_accept_after": 2, "refusal_accept_after": 2},
            "repeat_keys": rks,
            "slots": [{"slot_id": s, "units": [], "choices": [], "yes_no_map": None, "required": True,
                       "reask_sentence": "다시 말씀해 주세요.", "risk_rule": None} for s in slots]})
    return {"version": version, "questions": questions,
            "session_settings": {"risk_phrases": [], "confirm_limit": 2, "unrecognized_streak_limit": 3, "ask_limit": 3,
                                 "fixed_sentences": [], "guide_list": []}}
