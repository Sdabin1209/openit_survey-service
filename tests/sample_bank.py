"""D74 시험용 문항 세트: 학습 데이터셋 v2의 31개 문항 ID와 칸(시험용 값이며 실제 문항이 아니다).

질문 문장은 일부만 학습 데이터 표본에서 인용했고 나머지는 시험용 문장이다. 문항 순서는 문진 스키마의 QuestionFlow와 같다."""
from __future__ import annotations

import survey_schema

FIXED = {
    "ASK_REPEAT": "다시 한 번 말씀해 주세요.", "OOS_NOTICE": "문진과 관계없는 말씀은 도와드리기 어렵습니다.",
    "CALL_STAFF": "직원을 불러 드리겠습니다. 잠시만 기다려 주세요.", "FAREWELL": "문진이 끝났습니다. 수고하셨습니다.",
    "ASK_CONFIRM": "어느 쪽이 맞는지 말씀해 주세요.", "ASK_CORRECTION_TARGET": "어느 항목을 고칠까요?",
    "ASK_FINISH": "다 여쭤봤습니다. 이대로 마칠까요?", "RISK_NOTICE": "바로 직원을 불렀습니다. 그 자리에 계셔 주세요.",
    "RESUME": "다시 이어서 여쭙겠습니다.", "SURVEY_STOPPED": "문진을 여기서 마치겠습니다.",
    "WAIT_NOTICE": "잠시만 기다려 주세요.", "ASK_STOP": "그만하시겠어요?", "STAFF_NOTICE": "직원을 불러 드렸습니다. 문진은 이어서 하겠습니다.",
    "DEFERRED": "나중에 다시 여쭙겠습니다.",
}
BEVERAGES = ["soju", "beer", "wine", "spirits", "makgeolli"]
SENTENCES = {
    "Q1.D01": "혹시 뇌졸중으로 진단받으셨거나 현재 약물 치료 중이신가요?",
    "Q1.D02": "혹시 심근경색이나 협심증으로 진단받으셨거나 현재 약물 치료 중이신가요?",
    "Q1.D03": "다음은 고혈압입니다. 진단받은 적이 있으신지, 지금 약을 드시는지 알려 주세요.",
    "Q4": "일반담배를 평생 5갑(100개비) 이상 피운 적이 있으신가요?",
    "Q4_1": "일반담배를 지금 피우는지, 피운 기간, 하루 개비 수, 끊은 지 얼마나 됐는지 알려 주세요.",
    "Q7_2": "최근 1년 동안 하루 동안 가장 많이 마신 음주는 어느 정도였나요?",
    "Q10": "최근 1주일 동안 팔굽혀펴기, 윗몸일으키기, 아령 같은 근력 운동을 한 날은 며칠인가요?",
}


def _slots(qid: str) -> list[str]:
    if qid.startswith("Q1.D"):
        return ["diagnosed", "on_medication"]
    if qid.startswith("Q2.D"):
        return ["family_history"]
    return {"Q3": ["answer"], "Q4": ["answer"], "Q5": ["answer"], "Q6": ["answer"],
            "Q4_1": ["status", "total_years", "daily_count", "years_since_quit"],
            "Q5_1": ["status", "total_years", "daily_count", "years_since_quit"], "Q6_1": ["use_pattern"],
            "Q7": ["does_not_drink", "frequency", "unit"], "Q7_1": ["amount", "unit"], "Q7_2": ["amount", "unit"],
            "Q8_1": ["days"], "Q9_1": ["days"], "Q10": ["days"], "Q8_2": ["duration_minutes"], "Q9_2": ["duration_minutes"]}[qid]


def sample_bank(version: int = 1) -> dict:
    order = survey_schema.load_data("question_flow")["order"]
    questions = []
    for i, qid in enumerate(order, start=1):
        questions.append({
            "question_id": qid, "name": qid, "order": i, "prerequisite": None,
            "question_sentence": SENTENCES.get(qid, f"{qid} 문항입니다. 알려 주세요."),
            "refusal_explanation": "답하기 어려우시면 넘어가셔도 됩니다.", "guide_sentences": [], "affected_followups": [],
            "policy": {"question_limit": 3, "unknown_accept_after": 2, "refusal_accept_after": 2},
            "repeat_keys": BEVERAGES if qid in ("Q7_1", "Q7_2") else [],
            "slots": [{"slot_id": s, "units": [], "choices": [], "yes_no_map": None, "required": True,
                       "reask_sentence": f"{s}을(를) 다시 말씀해 주세요.", "risk_rule": None} for s in _slots(qid)]})
    return {"version": version, "questions": questions,
            "session_settings": {"risk_phrases": [], "confirm_limit": 2, "unrecognized_streak_limit": 2, "ask_limit": 3,
                                 "fixed_sentences": [{"key": k, "sentence": v} for k, v in FIXED.items()], "guide_list": []}}
