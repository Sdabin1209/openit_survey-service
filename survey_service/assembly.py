"""컴포넌트 입력 조립. 저장소 문서와 문항 세트에서 정해진 범위를 '고르기만' 한다(판단 없음).

범위는 docs/design.md 3.1의 표이고, 원본 출처 표가 없어 이 영역이 정한 해석이다(CR-004). 바꿀 일이 생기면 이 파일만 바꾼다.
"""
from __future__ import annotations

import copy
from typing import Iterable, Mapping, Optional

from .store.effects import ordered_questions, slot_key


class Bank:
    """문항 세트(QuestionBank dict)를 찾아보기 쉽게 감싼다. 원본을 바꾸지 않는다."""

    def __init__(self, bank: Mapping) -> None:
        self.raw = bank
        self.questions = ordered_questions(bank)
        self.by_id = {q["question_id"]: q for q in self.questions}
        self.settings = bank["session_settings"]

    def defs(self, qids: Iterable[str]) -> list[dict]:
        want = set(qids)
        return [copy.deepcopy(q) for q in self.questions if q["question_id"] in want]

    def prerequisite_question(self, qid: str) -> Optional[str]:
        q = self.by_id.get(qid)
        if q and q.get("prerequisite"):
            return q["prerequisite"]["slot"]["question_id"]
        return None

    def question_slots(self) -> list[dict]:
        return [{"question_id": q["question_id"], "slot_ids": [s["slot_id"] for s in q["slots"]]} for q in self.questions]


def _target_qids(targets: Iterable[Optional[Mapping]]) -> list[str]:
    out: list[str] = []
    for t in targets:
        if t and t["question_id"] not in out:
            out.append(t["question_id"])
    return out


def awaiting_of(last_directive: Optional[Mapping]) -> Optional[Mapping]:
    return last_directive.get("awaiting") if last_directive else None


def awaiting_question_ids(last_directive: Optional[Mapping]) -> list[str]:
    aw = awaiting_of(last_directive)
    return _target_qids(aw["targets"]) if aw else []


def parser_input(utterance: str, doc: Mapping, bank: Bank) -> dict:
    prog = doc["progress_record"]
    answered = []
    for r in doc["answer_records"]:
        cv = r["current_value"]
        if isinstance(cv, dict) and "answer" in cv:
            e = {"slot": copy.deepcopy(r["slot"]), "value": copy.deepcopy(cv["answer"])}
            if r.get("value_metadata"):
                e["value_metadata"] = copy.deepcopy(r["value_metadata"])
            answered.append(e)
    return {
        "utterance": utterance,
        "last_directive": copy.deepcopy(prog["last_directive"]),
        "awaiting_questions": bank.defs(awaiting_question_ids(prog["last_directive"])),
        "question_names": [{"question_id": q["question_id"], "name": q["name"], "slots": [s["slot_id"] for s in q["slots"]]}
                           for q in bank.questions],
        "answered_slots": answered,
        "guide_list": [{"guide_id": g["guide_id"], "selection_criterion": g["selection_criterion"]}
                       for g in bank.settings["guide_list"]],
    }


def _related_question_ids(items: list, last_directive: Optional[Mapping], bank: Bank) -> list[str]:
    qids = _target_qids(i.get("target") for i in items)
    for q in list(qids):
        pre = bank.prerequisite_question(q)
        if pre and pre not in qids:
            qids.append(pre)
    for q in awaiting_question_ids(last_directive):
        if q not in qids:
            qids.append(q)
    return qids


def normalizer_input(items: list, doc: Mapping, bank: Bank, turn_index: int) -> dict:
    prog = doc["progress_record"]
    qids = set(_related_question_ids(items, prog["last_directive"], bank))
    current, pendings = [], []
    for r in doc["answer_records"]:
        if r["slot"]["question_id"] not in qids:
            continue
        e = {"slot": copy.deepcopy(r["slot"]), "current_value": copy.deepcopy(r["current_value"])}
        if r.get("value_metadata"):
            e["value_metadata"] = copy.deepcopy(r["value_metadata"])
        current.append(e)
        p = r["pending"]
        if p:
            pe = {"pending_id": p["pending_id"], "slot": copy.deepcopy(r["slot"]), "value": copy.deepcopy(p["value"])}
            if p.get("value_metadata"):
                pe["value_metadata"] = copy.deepcopy(p["value_metadata"])
            pendings.append(pe)
    aw = awaiting_of(prog["last_directive"])
    return {
        "items": copy.deepcopy(items),
        "related_questions": bank.defs(qids),
        "current_values": current,
        "pendings": pendings,
        "awaiting": ({"action": aw["action"], "targets": copy.deepcopy(aw["targets"]),
                      "target_pending_id": aw["target_pending_id"]} if aw else None),
        "turn_index": turn_index,
    }


def judge_input(items: list, normalizer_output: Mapping, utterance: str, doc: Mapping, bank: Bank) -> dict:
    return {
        "items": copy.deepcopy(items),
        "item_checks": copy.deepcopy(normalizer_output["item_checks"]),
        "missing_required_slots": copy.deepcopy(normalizer_output["missing_required_slots"]),
        "utterance": utterance,
        "risk_phrases": list(bank.settings["risk_phrases"]),
        "last_directive": copy.deepcopy(doc["progress_record"]["last_directive"]),
        "pendings": [{"pending_id": r["pending"]["pending_id"], "slot": copy.deepcopy(r["slot"])}
                     for r in doc["answer_records"] if r["pending"]],
        "affected_followups": [{"question_id": q["question_id"], "followups": list(q["affected_followups"])}
                               for q in bank.questions if q["affected_followups"]],
        "question_slots": bank.question_slots(),
    }


def writer_input(judge_output: Mapping, doc: Mapping, bank: Bank) -> dict:
    qids = set(_target_qids(j["target"] for j in judge_output["judgments"]))
    qids.update(judge_output["invalidated_questions"])
    return {
        "judgments": copy.deepcopy(judge_output["judgments"]),
        "invalidated_questions": list(judge_output["invalidated_questions"]),
        "question_slots": bank.question_slots(),
        "target_records": [{"slot": copy.deepcopy(r["slot"]), "current_value": copy.deepcopy(r["current_value"]),
                            "pending": copy.deepcopy(r["pending"])}
                           for r in doc["answer_records"] if r["slot"]["question_id"] in qids],
    }


def planner_input(turn_kind: str, utterance_judgment: Optional[Mapping], judgments: list, store_summary: Optional[Mapping],
                  records_after_store: list, doc: Mapping, bank: Bank) -> dict:
    prog = doc["progress_record"]
    return {
        "turn_kind": turn_kind,
        "utterance_judgment": copy.deepcopy(utterance_judgment),
        "judgments": copy.deepcopy(judgments),
        "store_summary": copy.deepcopy(store_summary),
        "answer_records": copy.deepcopy(records_after_store),
        "last_directive": copy.deepcopy(prog["last_directive"]),
        "skipped": copy.deepcopy(prog["skipped"]),
        "confirm_stopped": copy.deepcopy(prog["confirm_stopped"]),
        "open_requests": copy.deepcopy(prog["open_requests"]),
        "planner_record": copy.deepcopy(doc["planner_record"]),
        "question_bank": copy.deepcopy(bank.raw),
    }


def generator_input(directive: Mapping, utterance: Optional[str]) -> dict:
    return {"directive": copy.deepcopy(directive), "utterance": utterance}


def dispatch_input(doc: Mapping) -> dict:
    return {
        "session_info": copy.deepcopy(doc["session_info"]),
        "answer_records": copy.deepcopy(doc["answer_records"]),
        "skipped": copy.deepcopy(doc["progress_record"]["skipped"]),
        "confirm_stopped": copy.deepcopy(doc["progress_record"]["confirm_stopped"]),
        "session_status": doc["progress_record"]["session_status"],
        "pause_history": copy.deepcopy(doc["progress_record"]["pause_history"]),
    }
