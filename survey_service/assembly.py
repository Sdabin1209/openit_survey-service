"""문항 세트 보조 객체와 결과 전달 입력 조립. 저장소 문서와 문항 세트에서 정해진 범위를 '고르기만' 한다(판단 없음).

D74로 컴포넌트(파서·정규화·판정·기록·Plan) 입력 조립은 없어졌다. A·B 입력 조립은 `extraction.py`가 한다.
"""
from __future__ import annotations

import copy
from typing import Mapping, Optional

from .store.effects import ordered_questions


class Bank:
    """문항 세트(QuestionBank dict)를 찾아보기 쉽게 감싼다. 원본을 바꾸지 않는다."""

    def __init__(self, bank: Mapping) -> None:
        self.raw = bank
        self.questions = ordered_questions(bank)
        self.by_id = {q["question_id"]: q for q in self.questions}
        self.settings = bank["session_settings"]

    def sentence(self, question_id: str) -> Optional[str]:
        """문항 은행의 질문 문장(표 찾기). 문항이 없으면 None."""
        q = self.by_id.get(question_id)
        return q["question_sentence"] if q else None

    def fixed_sentence(self, key: str) -> Optional[str]:
        for f in self.settings["fixed_sentences"]:
            if f["key"] == key:
                return f["sentence"]
        return None


def dispatch_input(doc: Mapping) -> dict:
    return {
        "session_info": copy.deepcopy(doc["session_info"]),
        "answer_records": copy.deepcopy(doc["answer_records"]),
        "skipped": copy.deepcopy(doc["progress_record"]["skipped"]),
        "session_status": doc["progress_record"]["session_status"],
        "pause_history": copy.deepcopy(doc["progress_record"]["pause_history"]),
    }
