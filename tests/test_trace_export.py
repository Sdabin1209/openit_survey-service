"""Trace 내보내기: outbox, 최소 1회 전달, UNAVAILABLE일 때만 재시도, 턴 처리와 분리."""
import threading
import time

import pytest
from boundary_contracts import BoundaryError

from survey_service.trace import Outbox, OutboxTraceSink, TraceExporter

from .e2e_support import Rig, item
from .fakes import FakeTraceIngest


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def parts(tmp_path):
    clock = Clock()
    ob = Outbox(str(tmp_path / "ob.db"))
    ing = FakeTraceIngest()
    ex = TraceExporter(ob, ing, base_delay=2.0, max_delay=8.0, clock=clock)
    sink = OutboxTraceSink(ob, clock)
    yield clock, ob, ing, ex, sink
    ob.close()


def run_turns(sink, n=3, **kw):
    rig = Rig(trace_sink=sink)
    rig.say(kind="SESSION_START")
    rig.say("안 피움", items=[item(0, "Q1", "상태", "안 피움", "안 피움")])
    if n > 2:
        rig.say(kind="SESSION_END")
    return rig


def test_envelope_carries_release_and_contract_version(parts):
    clock, ob, ing, ex, sink = parts
    run_turns(sink)
    r = ex.flush_once()
    assert r.sent == 3 and ing.calls == 3
    env = ing.received[("S1", "t1")]
    assert env["release_id"] == "rel-e2e-1" and env["runtime_contract_version"] == 1
    assert env["pipeline_schema_version"] == "1.1" and env["sent_at"].endswith("Z")
    assert env["trace"]["session_id"] == "S1" and env["trace"]["turn_id"] == "t1"
    assert ob.counts() == {"PENDING": 0, "SENT": 3, "DEAD": 0}


def test_turn_does_not_wait_for_ingest_and_ingest_down_does_not_fail_turns(parts):
    clock, ob, ing, ex, sink = parts
    ing.script = [BoundaryError("UNAVAILABLE", "내림")] * 50
    t0 = time.time()
    rig = run_turns(sink)
    assert time.time() - t0 < 5 and ing.calls == 0          # 턴을 처리하는 동안 전송을 부르지 않았다
    assert ob.counts()["PENDING"] == 3
    assert rig.doc()["progress_record"]["session_status"].startswith("ENDED")


def test_retries_only_on_unavailable_with_backoff_then_delivers(parts):
    clock, ob, ing, ex, sink = parts
    run_turns(sink)
    ing.script = [BoundaryError("UNAVAILABLE", "x"), BoundaryError("UNAVAILABLE", "x")]
    r = ex.flush_once()
    assert r.retried == 1 and r.paused and ing.calls == 1 and ob.counts()["PENDING"] == 3
    assert ex.flush_once().paused and ing.calls == 1             # 백오프 동안 부르지 않는다
    clock.t += 2.0                                               # base_delay
    assert ex.flush_once().retried == 1 and ing.calls == 2
    clock.t += 3.9                                               # 2배인 4초가 지나기 전
    assert ex.flush_once().paused and ing.calls == 2
    clock.t += 0.2
    r = ex.flush_once()
    assert r.sent == 3 and ob.counts() == {"PENDING": 0, "SENT": 3, "DEAD": 0} and ex.consecutive_unavailable == 0


def test_backoff_is_capped(parts):
    clock, ob, ing, ex, sink = parts
    run_turns(sink, 1)
    ing.script = [BoundaryError("UNAVAILABLE", "x")] * 10
    gaps = []
    for _ in range(6):
        ex.flush_once()
        gaps.append(ex.pause_until - clock.t)
        clock.t = ex.pause_until
    assert gaps == [2.0, 4.0, 8.0, 8.0, 8.0, 8.0]


@pytest.mark.parametrize("code", ["INVALID_RECORD", "UNSUPPORTED_VERSION", "PERMISSION_DENIED", "CONFLICT"])
def test_non_retryable_errors_are_not_retried(parts, code):
    clock, ob, ing, ex, sink = parts
    run_turns(sink, 2)
    ing.script = [BoundaryError(code, "거부")]
    r = ex.flush_once()
    assert r.dead == 1 and r.sent == 1 and ing.calls == 2          # 한 건은 재시도 없이 DEAD, 나머지는 계속 전송
    clock.t += 1000
    ex.flush_once()
    assert ing.calls == 2 and ob.counts() == {"PENDING": 0, "SENT": 1, "DEAD": 1}
    assert ob.dead_rows()[0]["error"].startswith(code)


def test_duplicate_counts_as_delivered(parts):
    clock, ob, ing, ex, sink = parts
    run_turns(sink, 2)
    ex.flush_once()
    env = ing.received[("S1", "t1")]
    ob.enqueue({**env, "turn_id": "t1"}, clock.t)                 # 같은 키: outbox가 이미 갖고 있어 새로 들어가지 않는다
    assert ob.counts()["PENDING"] == 0
    # 수집 쪽에는 이미 있는데 우리가 전달 확인을 못 받은 경우: 다시 보내면 DUPLICATE가 오고 전달 완료로 본다
    ob2 = ob
    ob2._db.execute("UPDATE outbox SET state='PENDING', next_attempt_at=0 WHERE turn_id='t1'")
    r = ex.flush_once()
    assert r.duplicate == 1 and ob.counts()["PENDING"] == 0


def test_restart_resends_what_was_not_confirmed(tmp_path):
    clock = Clock()
    path = str(tmp_path / "ob.db")
    ob = Outbox(path)
    run_turns(OutboxTraceSink(ob, clock))
    ob.close()
    ob = Outbox(path)                                               # 프로세스를 다시 띄운 것과 같다
    ing = FakeTraceIngest()
    r = TraceExporter(ob, ing, clock=clock).flush_once()
    assert r.sent == 3 and len(ing.received) == 3


def test_at_least_once_when_send_ok_but_confirmation_lost(parts):
    clock, ob, ing, ex, sink = parts
    run_turns(sink, 2)

    class LostAck:
        def __init__(self, inner):
            self.inner, self.first = inner, True

        def send(self, env):
            r = self.inner.send(env)
            if self.first:
                self.first = False
                raise BoundaryError("UNAVAILABLE", "응답이 오기 전에 연결이 끊김")
            return r
    ex.ingest = LostAck(ing)
    ex.flush_once()
    clock.t += 5
    ex.flush_once()
    assert len(ing.received) == 2 and ob.counts()["SENT"] == 2     # 두 번 보냈지만 수집에는 한 건씩만 있다
    assert ing.calls == 3


def test_locally_invalid_trace_is_not_sent(parts):
    clock, ob, ing, ex, sink = parts
    sink.submit("rel-1", {"session_id": "S9", "turn_id": "t9", "contract_version": 1})      # TurnTrace 필수 필드가 없다
    r = ex.flush_once()
    assert r.dead == 1 and ing.calls == 0 and "TurnTrace" in ob.dead_rows()[0]["error"]


def test_unexpected_exception_is_held_then_dead_after_limit(parts):
    clock, ob, ing, ex, sink = parts
    ex.max_unexpected = 3
    run_turns(sink, 1)                                  # 봉투 2건
    ing.script = [ConnectionError("끊김")] * 6
    ex.flush_once()
    assert ob.counts()["PENDING"] == 2 and ex.last_error == "UNEXPECTED: ConnectionError"
    for _ in range(2):
        clock.t += 100
        ex.flush_once()
    assert ob.counts() == {"PENDING": 0, "SENT": 0, "DEAD": 2} and ing.calls == 6 and not ing.received


def test_enqueue_is_idempotent(parts):
    clock, ob, ing, ex, sink = parts
    rig = run_turns(sink, 1)
    n = ob.counts()["PENDING"]
    sink.submit("rel-e2e-1", {"session_id": "S1", "turn_id": "t1", "contract_version": 1})
    assert ob.counts()["PENDING"] == n


def test_background_thread_delivers(parts):
    clock, ob, ing, ex, sink = parts
    ex.clock = time.time
    ex.start(interval=0.05)
    try:
        run_turns(OutboxTraceSink(ob, time.time))
        for _ in range(100):
            if ob.counts()["SENT"] == 3:
                break
            time.sleep(0.05)
        assert ob.counts()["SENT"] == 3
    finally:
        ex.stop()


def test_stats(parts):
    clock, ob, ing, ex, sink = parts
    run_turns(sink, 2)
    clock.t += 5
    s = ex.stats()
    assert s["pending"] == 2 and s["oldest_pending_age_seconds"] == 5.0


# ---------------------------------------------------------------- 외부 ID를 봉투의 Id 형식으로 (CONVENTIONS 10절)
import hashlib
import re

from survey_service.trace import envelope_id


def test_envelope_id_keeps_valid_ids_and_hashes_others():
    for ok in ["S-TEST", "a", "A.b_c:d-e", "x" * 128]:
        assert envelope_id(ok) == ok
    for bad in ["세션 1", "has space", "-leading", "", "x" * 129, "a/b", "é"]:
        out = envelope_id(bad)
        assert out == "x-" + hashlib.sha256(bad.encode("utf-8")).hexdigest()[:32]
        assert re.fullmatch(r"x-[0-9a-f]{32}", out) and out == envelope_id(bad)


def test_non_id_session_and_turn_ids_are_delivered_not_lost(parts):
    clock, ob, ing, ex, sink = parts
    rig = Rig(trace_sink=sink)
    rig.say(kind="SESSION_START", sid="세션 하나", turn_id="턴 1")
    rig.say(kind="SESSION_END", sid="세션 하나", turn_id="turn-2")
    r = ex.flush_once()
    assert r.sent == 2 and r.dead == 0 and ob.counts() == {"PENDING": 0, "SENT": 2, "DEAD": 0}
    sid = envelope_id("세션 하나")
    env = ing.received[(sid, envelope_id("턴 1"))]
    assert env["session_id"] == sid and sid.startswith("x-")
    assert env["trace"]["session_id"] == "세션 하나" and env["trace"]["turn_id"] == "턴 1"   # 원본은 본문에 남는다
    assert (sid, "turn-2") in ing.received                                                  # 맞는 ID는 그대로


def test_converted_ids_keep_idempotency(parts):
    clock, ob, ing, ex, sink = parts
    sink.submit("rel-1", {"session_id": "세션", "turn_id": "턴", "contract_version": 1})
    sink.submit("rel-1", {"session_id": "세션", "turn_id": "턴", "contract_version": 1})
    assert ob.counts()["PENDING"] == 1
