"""Session Store 적합성 시험: 메모리와 SQLite가 같은 시험을 통과한다."""
import copy
import sqlite3
import threading

import pytest

from survey_service.errors import StoreError
from survey_service.store import effects
from survey_service.store.memory import MemorySessionStore
from survey_service.store.sqlite import SqliteSessionStore
from .skeleton_bank import skeleton_bank


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemorySessionStore()
    else:
        s = SqliteSessionStore(str(tmp_path / "s.db"))
        yield s
        s.close()


def writes(value=None):
    sr = effects.empty_store_result()
    if value is not None:
        sr["store_decisions"] = [{"slot": {"question_id": "Q5", "repeat_key": "", "slot_id": "시간"}, "action": "FIRST_WRITE",
                                  "value": value, "pending_id": None, "risk_mark": None}]
    upd = {"last_directive": {"action": "ASK_NEXT", "targets": [], "target_pending_id": None, "awaiting": None, "say_values": [],
                              "reask_reasons": [], "notices": [], "sentence_materials": {}},
           "open_requests": {"correction_requests": [], "stop_requested": False, "deferred": []}, "skipped_add": [],
           "skipped_remove": [], "confirm_stopped_add": [], "confirm_stopped_remove": [], "session_status": None, "pause_history_add": []}
    plan = {"attempt_counts": [], "confirm_counts": [], "unknown_counts": [], "refusal_counts": [], "unrecognized_streak": 0, "ask_counts": []}
    return {"store_result": sr, "progress_record_update": upd, "planner_record_update": plan}


def make(store, sid="S1", release="rel-1"):
    st = effects.initial_state(sid, skeleton_bank())
    rev = store.create(sid, release, st)
    return st, rev


def test_not_found(store):
    r = store.load("nope")
    assert r == {"session_id": "nope", "status": "NOT_FOUND", "revision": None, "state": None}
    assert store.release_id_of("nope") is None


def test_create_load_roundtrip_and_isolation(store):
    st, rev = make(store)
    r = store.load("S1")
    assert r["status"] == "OK" and r["revision"] == rev and r["state"] == st
    r["state"]["answer_records"].clear()                       # 돌려받은 값을 바꿔도 저장소는 그대로
    assert len(store.load("S1")["state"]["answer_records"]) > 0
    assert store.release_id_of("S1") == "rel-1"
    with pytest.raises(StoreError):
        store.create("S1", "rel-1", st)                        # 이미 있다


def test_commit_applies_all_and_bumps_revision(store):
    st, rev = make(store)
    res = store.commit({"session_id": "S1", "expected_revision": rev, "writes": writes({"amount": 7, "unit": "시간"})},
                       turn_id="t1", input_hash="h1")
    assert res["status"] == "OK" and res["write_outcome"] == "APPLIED" and res["revision"] == rev + 1
    doc = store.load("S1")["state"]
    q5 = [r for r in doc["answer_records"] if r["slot"]["question_id"] == "Q5"][0]
    assert q5["current_value"] == {"answer": {"amount": 7, "unit": "시간"}}
    assert doc["progress_record"]["last_directive"]["action"] == "ASK_NEXT"
    t = store.get_turn("S1", "t1")
    assert t.committed and t.input_hash == "h1" and t.result is None


def test_commit_revision_conflict_applies_nothing(store):
    st, rev = make(store)
    res = store.commit({"session_id": "S1", "expected_revision": rev + 5, "writes": writes({"amount": 7, "unit": "시간"})},
                       turn_id="t1", input_hash="h1")
    assert res["status"] == "REVISION_CONFLICT" and res["write_outcome"] == "NOT_APPLIED" and res["revision"] == rev
    assert store.load("S1")["state"] == st and store.get_turn("S1", "t1") is None


def test_commit_unappliable_write_applies_nothing(store):
    st, rev = make(store)
    w = writes({"amount": 7, "unit": "시간"})
    w["store_result"]["store_decisions"].append({"slot": {"question_id": "Q9", "repeat_key": "", "slot_id": "x"}, "action": "FIRST_WRITE",
                                                 "value": "a", "pending_id": None, "risk_mark": None})
    res = store.commit({"session_id": "S1", "expected_revision": rev, "writes": w}, turn_id="t1", input_hash="h1")
    assert res["status"] == "DB_ERROR" and res["write_outcome"] == "NOT_APPLIED"
    assert store.load("S1")["state"] == st                      # 앞의 결정도 반영되지 않았다


def test_commit_unknown_session(store):
    res = store.commit({"session_id": "zz", "expected_revision": 1, "writes": writes()}, turn_id="t1", input_hash="h")
    assert res["status"] == "DB_ERROR" and res["write_outcome"] == "NOT_APPLIED"


def test_append_log_and_dispatch(store):
    make(store)
    store.append_log("S1", {"n": 1})
    store.append_log("S1", {"n": 2})
    store.append_dispatch("S1", {"succeeded": True})
    doc = store.load("S1")["state"]
    assert doc["dialogue_log"] == [{"n": 1}, {"n": 2}] and doc["dispatch_log"] == [{"succeeded": True}]
    with pytest.raises(StoreError):
        store.append_log("none", {"n": 1})


def test_turn_result_roundtrip(store):
    store.save_turn_result("S1", "t9", "hh", {"orchestrator_output": {"a": 1}})
    t = store.get_turn("S1", "t9")
    assert t.result == {"orchestrator_output": {"a": 1}} and t.input_hash == "hh" and not t.committed
    make(store)
    res = store.commit({"session_id": "S1", "expected_revision": 1, "writes": writes()}, turn_id="t1", input_hash="a")
    store.save_turn_result("S1", "t1", "a", {"x": 1})
    t = store.get_turn("S1", "t1")
    assert t.committed and t.result == {"x": 1}


def test_open_release_ids(store):
    make(store, "A", "rel-1")
    make(store, "B", "rel-2")
    make(store, "C", "rel-3")
    w = writes()
    w["progress_record_update"]["session_status"] = "ENDED_COMPLETE"
    store.commit({"session_id": "C", "expected_revision": 1, "writes": w}, turn_id="t", input_hash="h")
    w2 = writes()
    w2["progress_record_update"]["session_status"] = "PAUSED_RISK"
    store.commit({"session_id": "B", "expected_revision": 1, "writes": w2}, turn_id="t", input_hash="h")
    assert store.open_release_ids() == {"rel-1", "rel-2"}


def test_concurrent_commit_only_one_wins(store):
    st, rev = make(store)
    results = []

    def go(i):
        results.append(store.commit({"session_id": "S1", "expected_revision": rev, "writes": writes({"amount": i, "unit": "시간"})},
                                    turn_id=f"t{i}", input_hash=f"h{i}")["status"])
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["OK"] + ["REVISION_CONFLICT"] * 7


def test_sqlite_persists_across_reopen(tmp_path):
    p = str(tmp_path / "x.db")
    s1 = SqliteSessionStore(p)
    st, rev = make(s1)
    s1.commit({"session_id": "S1", "expected_revision": rev, "writes": writes({"amount": 7, "unit": "시간"})}, turn_id="t1", input_hash="h")
    s1.close()
    s2 = SqliteSessionStore(p)
    assert s2.load("S1")["revision"] == rev + 1 and s2.get_turn("S1", "t1").committed and s2.release_id_of("S1") == "rel-1"
    s2.close()


def test_sqlite_db_error_is_reported_not_raised(tmp_path):
    s = SqliteSessionStore(str(tmp_path / "x.db"))
    make(s)
    s._db.close()
    assert s.load("S1")["status"] == "DB_ERROR"
    res = s.commit({"session_id": "S1", "expected_revision": 1, "writes": writes()}, turn_id="t", input_hash="h")
    assert res["status"] == "DB_ERROR" and res["write_outcome"] == "NOT_APPLIED"
    with pytest.raises(StoreError):
        s.ping()


def test_sqlite_unwritable_path():
    with pytest.raises(StoreError):
        SqliteSessionStore("/nonexistent-dir/x/y.db")
