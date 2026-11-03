"""문진표 여러 개(ADR-0057): 흐름은 문진표(문항 order·prerequisite·affected_followups·slots)에서 파생하고, 세션마다 문진표를 고른다."""
import copy
import itertools
import json
from pathlib import Path

import pytest

from survey_service.components.flow_table import FlowTable, build_flow, flow_problems
from survey_service.wiring.ports import LoadedRelease

from .e2e_support import ext, val
from .sample_bank import sample_bank
from .test_results_signals_retention import AUTH, RESULT, RESULT_V, VIEW_V, Env

SET_A = dict(question_set_id="set-a", title="A 문진표", order=["Q3", "Q4", "Q4_1", "Q5"])
SET_B = dict(question_set_id="set-b", title="B 문진표", order=["Q5", "Q3", "Q4"])


def two_sets(env):
    """Env의 릴리스를 문진표 둘로 바꾼다."""
    rig = env.rig
    rel = LoadedRelease("rel-e2e-1", {}, {s["question_set_id"]: sample_bank(**s) for s in (SET_A, SET_B)}, rig.session)
    rig.registry.register(rel, activate=True)
    return rel


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()


# ---------------------------------------------------------------- (a) 문진표마다 첫 문항·다음 문항이 다르다
def test_two_sets_with_different_order_give_different_first_and_next_items(env):
    rel = two_sets(env)
    flows = {qid: env.rig.orch.question_set(rel, qid)[1].table for qid in ("set-a", "set-b")}
    assert flows["set-a"].order[0] == "Q3" and flows["set-b"].order[0] == "Q5"
    assert env.start("KA", question_set_id="set-a")[0] == 200
    assert env.start("KB", question_set_id="set-b")[0] == 200
    cur = lambda sid: env.rig.doc(sid)["progress_record"]["current_question_id"]
    assert (cur("KA"), cur("KB")) == ("Q3", "Q5")                                   # 첫 문항이 세트마다 다르다
    firsts = [r["next"]["question_id"] for r in env.rig.session.respond_requests]
    assert firsts == ["Q3", "Q5"]                                                   # 시작 응답 요청의 첫 문항도 그렇다
    env.say("KA", "네", "t-a", out=ext("NEXT", "Q4", val("Q3.answer", "yes")))
    env.say("KB", "네", "t-b", out=ext("NEXT", "Q3", val("Q5.answer", True)))
    assert (cur("KA"), cur("KB")) == ("Q4", "Q3")                                   # 다음 문항도 다르다
    assert env.rig.store.question_set_of("KA") == ("set-a", "A 문진표") and env.rig.store.question_set_of("KB") == ("set-b", "B 문진표")
    assert flows["set-a"].next_after("Q4", {"Q4.answer": True}) == "Q4_1" and flows["set-b"].next_after("Q4", {}) == "END"      # 건너뛰기·끝도 세트의 것


# ---------------------------------------------------------------- (b) prerequisite 6개 연산자
CASES = [   # (op, value, 저장 값, 묻는가)
    ("==", True, True, True), ("==", True, False, False), ("==", 1, True, False),             # bool과 수는 다르다
    ("!=", 0, 1, True), ("!=", 0, 0, False),
    (">", 5, 6, True), (">", 5, 5, False),
    (">=", 5, 5, True), (">=", 5, 4, False),
    ("<", 5, 4, True), ("<", 5, 5, False),
    ("<=", 5, 5, True), ("<=", 5, 6, False),
]


def gated_bank(op, value):
    b = sample_bank(order=["Q3", "Q4", "Q5"])
    b["questions"][1]["prerequisite"] = {"slot": {"question_id": "Q3", "repeat_key": "", "slot_id": "answer"}, "op": op, "value": value}
    return b


@pytest.mark.parametrize("op,value,saved,asked", CASES)
def test_prerequisite_false_skips_the_item_and_true_asks_it(op, value, saved, asked):
    flow = build_flow(gated_bank(op, value))
    assert flow.table.next_after("Q3", {"Q3.answer": saved}) == ("Q4" if asked else "Q5")


@pytest.mark.parametrize("op", ["==", "!=", ">", ">=", "<", "<="])
def test_no_saved_value_means_the_item_is_asked(op):
    flow = build_flow(gated_bank(op, 5))
    assert flow.table.next_after("Q3", {}) == "Q4"


def test_skip_slot_uses_repeat_key_notation_and_op_is_negated():
    b = sample_bank(order=["Q7", "Q7_1", "Q8_1"])
    b["questions"][2]["prerequisite"] = {"slot": {"question_id": "Q7_1", "repeat_key": "soju", "slot_id": "amount"}, "op": ">=", "value": 2}
    assert build_flow(b).table.skips[-1] == {"slot": "Q7_1[soju].amount", "op": "<", "value": 2, "skip_item": "Q8_1"}


def test_duplicate_order_cannot_make_a_flow():
    b = sample_bank(order=["Q3", "Q4"])
    b["questions"][1]["order"] = b["questions"][0]["order"]
    assert flow_problems(b) and "겹친다" in flow_problems(b)[0]


# ---------------------------------------------------------------- (c) question_set_id 처리
def test_unknown_set_is_400_and_single_set_may_omit(env):
    assert env.call("GET", "/v1/question-sets", auth=AUTH)[1]["items"] == [{"question_set_id": "general-intake", "title": "일반 문진표", "version": "1"}]
    assert env.start("K0")[0] == 200                                       # 하나면 생략 가능
    st, body, _ = env.start("K9", question_set_id="nope")
    assert st == 400 and body["code"] == "INVALID_RECORD" and body["details"]["reason"] == "UNKNOWN_QUESTION_SET"
    assert env.rig.store.load("K9")["status"] == "NOT_FOUND"


def test_two_sets_require_the_id(env):
    two_sets(env)
    st, body, _ = env.call("GET", "/v1/question-sets", auth=AUTH)
    assert st == 200 and body["items"] == [{"question_set_id": "set-a", "title": "A 문진표", "version": "1"},
                                           {"question_set_id": "set-b", "title": "B 문진표", "version": "1"}]
    st, body, _ = env.start("K1")
    assert st == 400 and body["details"]["reason"] == "QUESTION_SET_REQUIRED"
    assert env.start("K1", question_set_id="zzz")[0] == 400
    assert env.start("K1", question_set_id="set-b")[0] == 200
    assert env.call("GET", "/v1/question-sets", auth="Bearer wrong")[0] == 401


# ---------------------------------------------------------------- (d) 결과에 문진표 ID·제목
def test_results_carry_set_id_and_title(env):
    two_sets(env)
    env.start("K1", question_set_id="set-b")
    st, r, _ = env.call("GET", "/v1/results/K1", auth=RESULT)
    assert st == 200 and not list(RESULT_V.iter_errors(r)) and (r["question_set_id"], r["question_set_title"]) == ("set-b", "B 문진표")
    st, lst, _ = env.call("GET", "/v1/results", auth=RESULT)
    assert [(i["question_set_id"], i["question_set_title"]) for i in lst["items"]] == [("set-b", "B 문진표")]
    st, v, _ = env.call("GET", "/v1/staff/sessions/K1")
    assert not list(VIEW_V.iter_errors(v)) and v["question_set_id"] == "set-b" and v["question_set_title"] == "B 문진표"


# ---------------------------------------------------------------- (e) 회귀: 옛 흐름 파일의 7개 건너뛰기를 prerequisite로 옮긴 문진표
def test_bank_with_the_seven_prerequisites_gives_the_same_next_items_as_the_legacy_flow_file():
    legacy = json.loads((Path(__file__).parent / "fixtures_legacy_question_flow.json").read_text(encoding="utf-8"))
    old = FlowTable(order=tuple(legacy["order"]), skips=tuple(legacy["skips"]))
    new = build_flow(sample_bank()).table
    assert new.order == old.order and len(new.skips) == 7
    slots = sorted({r["slot"] for r in legacy["skips"]})
    values = [None, True, False, 0, 1, 2]
    n = 0
    for combo in itertools.product(values, repeat=len(slots)):
        saved = {s: v for s, v in zip(slots, combo) if v is not None}
        for item in legacy["order"]:
            assert new.next_after(item, saved) == old.next_after(item, saved)
            n += 1
    assert n > 1000


# ---------------------------------------------------------------- Trace 봉투의 문진표(FailureCase.question_set의 재료)
def test_trace_envelope_carries_the_sessions_question_set_ref(env):
    from boundary_contracts import validator
    from survey_service.components.trace_envelope import OutboxTraceSink
    rel = two_sets(env)
    rel.manifest.update(question_sets=[{"id": q, "version": "1", "content_hash": "sha256:" + str(i) * 64, "title": "t"} for i, q in enumerate(("set-a", "set-b"))])
    env.start("KB", question_set_id="set-b")
    ref = env.rig.orch._set_ref("rel-e2e-1", {"session_id": "KB"})
    assert ref == {"id": "set-b", "version": "1", "content_hash": "sha256:" + "1" * 64}      # 활성 릴리스의 첫 세트가 아니라 이 세션의 세트
    trace = {"contract_version": 2, "session_id": "KB", "turn_id": "t1"}
    envl = OutboxTraceSink(None, lambda: 0).envelope("rel-e2e-1", trace, ref)
    assert envl["ext"] == {"survey-service": {"question_set": ref}}
    assert envl["trace"] is trace and "ext" not in OutboxTraceSink(None, lambda: 0).envelope("rel-e2e-1", trace)
    assert list(validator("TraceEnvelope").iter_errors(envl)) == []
