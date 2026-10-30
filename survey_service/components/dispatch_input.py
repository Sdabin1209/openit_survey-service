"""결과 전달 입력(DispatchInput) 고르기. 입력 SessionDocument -> 출력 DispatchInput(문진 스키마). 판단 없음."""
from __future__ import annotations

import copy
from typing import Mapping


def dispatch_input(doc: Mapping) -> dict:
    return {
        "session_info": copy.deepcopy(doc["session_info"]),
        "answer_records": copy.deepcopy(doc["answer_records"]),
        "skipped": copy.deepcopy(doc["progress_record"]["skipped"]),
        "session_status": doc["progress_record"]["session_status"],
        "pause_history": copy.deepcopy(doc["progress_record"]["pause_history"]),
    }
