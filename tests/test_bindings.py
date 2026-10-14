"""입력 조립을 docs/rules/sources/bindings.yaml(필드별 원천·시점·전달 방식)과 필드 단위로 대조한다."""
import copy
from pathlib import Path

import pytest
import yaml

from survey_service.store import effects

from .e2e_support import Rig, item, other

B = yaml.safe_load((Path(__file__).parents[1] / "docs/rules/sources/bindings.yaml").read_text(encoding="utf-8"))["inputs"]
TRACE_KEY = {"UtteranceParserInput": "parser_input", "AnswerNormalizerInput": "normalizer_input", "AnswerJudgeInput": "judge_input",
             "RecordWriterInput": "record_writer_input", "DialoguePlannerInput": "planner_input",
             "ResponseGeneratorInput": "generator_input", "DispatchInput": "dispatch_input", "OrchestratorInput": "orchestrator_input",
             "SessionLoadRequest": "persistence_load_request", "SessionCommitRequest": "persistence_commit_request",
             "FrontendInput": "frontend_input", "StaffConsoleInput": "staff_console_input"}


@pytest.fixture(scope="module")
def conflict_turn():
    rig = Rig()
    rig.say(kind="SESSION_START")
    rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")])
    rig.say("일곱 시간", items=[item(0, "Q5", "시간", "일곱 시간", "일곱 시간")])
    before = rig.doc()
    o = rig.say("8시간", items=[item(0, "Q5", "시간", "8시간", "8시간")])        # 충돌 -> 확인 대기
    pre = before
    # 확인 대기가 있는 상태에서 한 턴 더(pendings가 채워진 입력을 얻는다)
    before2 = rig.doc()
    o2 = rig.say("네", items=[other(0, "CONFIRM_YES", "네")])
    end = rig.say(kind="SESSION_END")
    return rig, pre, o, before2, o2, end


@pytest.mark.parametrize("comp", ["UtteranceParserInput", "AnswerNormalizerInput", "AnswerJudgeInput", "RecordWriterInput",
                                  "DialoguePlannerInput", "ResponseGeneratorInput"])
def test_every_bound_field_is_assembled_and_no_extra(conflict_turn, comp):
    rig, pre, o, *_ = conflict_turn
    assert set(o.trace[TRACE_KEY[comp]]) == set(B[comp]["fields"])


def test_other_components_cover_their_bound_fields(conflict_turn):
    rig, pre, o, b2, o2, end = conflict_turn
    assert set(end.trace["dispatch_input"]) == set(B["DispatchInput"]["fields"])
    assert set(o.trace["orchestrator_input"]) == set(B["OrchestratorInput"]["fields"])
    assert set(o.trace["persistence_load_request"]) == set(B["SessionLoadRequest"]["fields"])
    assert set(o.trace["persistence_commit_request"]) == set(B["SessionCommitRequest"]["fields"])
    assert set(o.frontend_input) == set(B["FrontendInput"]["fields"])
    # StaffConsoleInput: staff_alert는 PlannerOutput.staff_alert + session_id의 결합
    rig2 = Rig()
    rig2.say(kind="SESSION_START")
    r = rig2.say("숨이 안 쉬어져요", items=[other(0, "RISK_SIGNAL", "숨이 안 쉬어져요")])
    assert set(r.staff_console_input) == set(B["StaffConsoleInput"]["fields"])
    assert r.staff_console_input["staff_alert"] == {"session_id": "S1", "staff_alert": r.trace["planner_output"]["staff_alert"]}


def test_snapshots_turn_start_vs_post_store_write(conflict_turn):
    rig, pre, o, *_ = conflict_turn
    t = o.trace
    start = pre["answer_records"]
    # turn_start: 파서·정규화기·판정기·저장 판단은 이번 턴 쓰기 전의 값
    assert {(a["slot"]["slot_id"], str(a["value"])) for a in t["parser_input"]["answered_slots"]} == \
        {(r["slot"]["slot_id"], str(r["current_value"]["answer"])) for r in start if isinstance(r["current_value"], dict)}
    for cv in t["normalizer_input"]["current_values"]:
        assert cv["current_value"] == [r for r in start if r["slot"] == cv["slot"]][0]["current_value"]
    assert t["judge_input"]["last_directive"] == pre["progress_record"]["last_directive"]
    for tr in t["record_writer_input"]["target_records"]:
        assert tr == {k: [r for r in start if r["slot"] == tr["slot"]][0][k] for k in ("slot", "current_value", "pending")}
    # post_store_write: 플래너는 이번 턴 저장 결정까지 반영된 기록, 나머지 진행 기록은 턴 시작 값
    assert t["planner_input"]["answer_records"] == effects.apply_store_result(start, t["record_writer_output"])
    assert t["planner_input"]["answer_records"] != start
    assert t["planner_input"]["last_directive"] == pre["progress_record"]["last_directive"]
    assert t["planner_input"]["planner_record"] == pre["planner_record"]


def test_projections_follow_the_table(conflict_turn):
    rig, pre, o, b2, o2, end = conflict_turn
    bank = rig.bank
    t = o2.trace
    assert t["parser_input"]["question_names"] == [{"question_id": q["question_id"], "name": q["name"],
                                                    "slots": [s["slot_id"] for s in q["slots"]]} for q in bank["questions"]]
    assert t["parser_input"]["guide_list"] == []
    assert t["judge_input"]["affected_followups"] == [{"question_id": q["question_id"], "followups": q["affected_followups"]}
                                                      for q in bank["questions"]]            # 비어 있는 문항도 포함
    assert t["judge_input"]["question_slots"] == t["record_writer_input"]["question_slots"] == \
        [{"question_id": q["question_id"], "slot_ids": [s["slot_id"] for s in q["slots"]]} for q in bank["questions"]]
    assert t["judge_input"]["risk_phrases"] == bank["session_settings"]["risk_phrases"]
    assert t["planner_input"]["question_bank"] == bank
    # pendings: 정규화기는 slot, pending_id, value / 판정기는 slot, pending_id만 (risk_mark 없음)
    assert t["normalizer_input"]["pendings"] and set(t["normalizer_input"]["pendings"][0]) == {"pending_id", "slot", "value"}
    assert set(t["judge_input"]["pendings"][0]) == {"pending_id", "slot"}
    # awaiting: last_directive가 null이면 null, 아니면 그 awaiting(AnswerNormalizerInput 계약이 허용하는 필드만)
    aw = b2["progress_record"]["last_directive"]["awaiting"]
    assert t["normalizer_input"]["awaiting"] == {k: aw[k] for k in ("action", "targets", "target_pending_id")}
    first = o.trace["normalizer_input"]
    assert first["awaiting"] is not None
    assert o.trace["orchestrator_input"]["latest_question_bank_version"] == bank["version"]


def test_awaiting_is_null_without_last_directive():
    rig = Rig()
    o = rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")])      # 세션 없음 -> 생성 후 전체 흐름
    assert o.trace["normalizer_input"]["awaiting"] is None and o.trace["parser_input"]["awaiting_questions"] == []


def test_related_questions_selectors(conflict_turn):
    rig, pre, o, *_ = conflict_turn
    ids = [q["question_id"] for q in o.trace["normalizer_input"]["related_questions"]]
    assert ids == ["Q5", "Q6"]                              # item_target_questions(Q5) + awaiting_slot_questions(직전에 물은 Q6)
    rig2 = Rig()
    rig2.say(kind="SESSION_START")
    o2 = rig2.say("20개", items=[item(0, "Q2", "개비", "20개", "20개")])
    ids2 = [q["question_id"] for q in o2.trace["normalizer_input"]["related_questions"]]
    assert ids2 == ["Q1", "Q2"]                              # direct_prerequisite_questions(Q2의 선행 Q1) + 항목 대상 + awaiting(Q1)
