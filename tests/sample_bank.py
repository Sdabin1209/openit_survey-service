"""실제 turn-engine과 함께 돌려 보기 위한 작은 문항 세트(시험용 값이며 실제 문항이 아니다)."""
from __future__ import annotations

FIXED = {
    "ASK_REPEAT": "다시 한 번 말씀해 주세요.", "OOS_NOTICE": "문진과 관계없는 말씀은 도와드리기 어렵습니다.",
    "CALL_STAFF": "직원을 불러 드리겠습니다. 잠시만 기다려 주세요.", "FAREWELL": "문진이 끝났습니다. 수고하셨습니다.",
    "ASK_CONFIRM": "어느 쪽이 맞는지 말씀해 주세요.", "ASK_CORRECTION_TARGET": "어느 항목을 고칠까요?",
    "ASK_FINISH": "다 여쭤봤습니다. 이대로 마칠까요?", "RISK_NOTICE": "바로 직원을 불렀습니다. 그 자리에 계셔 주세요.",
    "RESUME": "다시 이어서 여쭙겠습니다.", "SURVEY_STOPPED": "문진을 여기서 마치겠습니다.",
    "WAIT_NOTICE": "잠시만 기다려 주세요.", "ASK_STOP": "그만하시겠어요?", "STAFF_NOTICE": "직원을 불러 드렸습니다. 문진은 이어서 하겠습니다.",
    "DEFERRED": "나중에 다시 여쭙겠습니다.",
}


def _slot(slot_id, *, units=(), choices=(), yes_no=None, risk=None):
    return {"slot_id": slot_id, "units": list(units), "choices": list(choices), "yes_no_map": yes_no, "required": True,
            "reask_sentence": f"{slot_id}을(를) 다시 말씀해 주세요.", "risk_rule": risk}


def _q(qid, name, order, sentence, slots, *, prerequisite=None, followups=()):
    return {"question_id": qid, "name": name, "order": order, "prerequisite": prerequisite, "question_sentence": sentence,
            "refusal_explanation": "답하기 어려우시면 넘어가셔도 됩니다.", "guide_sentences": [],
            "affected_followups": list(followups), "policy": {"question_limit": 3, "unknown_accept_after": 2, "refusal_accept_after": 2},
            "repeat_keys": [], "slots": slots}


def sample_bank(version: int = 1) -> dict:
    return {
        "version": version,
        "questions": [
            _q("Q1", "흡연", 1, "담배는 피우시나요?", [_slot("상태", choices=["현재 피움", "끊음", "안 피움"])], followups=["Q2"]),
            _q("Q2", "흡연량", 2, "하루에 몇 개비나 피우세요?",
               [_slot("개비", units=[{"name": "개비", "synonyms": ["개", "개비"], "range": [1, 100], "convert_to": None}])],
               prerequisite={"slot": {"question_id": "Q1", "repeat_key": "", "slot_id": "상태"}, "value": "현재 피움"}),
            _q("Q5", "수면", 3, "하루에 몇 시간 주무세요?",
               [_slot("시간", units=[{"name": "시간", "synonyms": [], "range": [0, 24], "convert_to": None}])]),
            _q("Q6", "자살 생각", 4, "최근에 죽고 싶다는 생각을 하신 적이 있나요?",
               [_slot("여부", choices=["예", "아니요"],
                      risk={"operator": "=", "value": "예", "response": "IMMEDIATE"})]),
            _q("Q8", "운동", 5, "일주일에 몇 번이나 운동하세요?",
               [_slot("횟수", units=[{"name": "회", "synonyms": ["번"], "range": [0, 14], "convert_to": None}])]),
        ],
        "session_settings": {
            "risk_phrases": ["숨이 안 쉬어져요", "가슴이 너무 아파"], "confirm_limit": 2, "unrecognized_streak_limit": 2,
            "ask_limit": 3, "fixed_sentences": [{"key": k, "sentence": v} for k, v in FIXED.items()], "guide_list": []},
    }
