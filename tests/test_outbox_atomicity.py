"""Trace outbox는 세션 commit과 같은 DB 트랜잭션에 쓴다(H02, 표준: Transactional Outbox).

기대값의 출처(이 시험만을 근거로 삼지 않는다):
- Transactional Outbox 패턴(microservices.io, Chris Richardson): 업무 데이터 변경과 메시지(outbox 행)를 **같은 로컬 트랜잭션**으로 쓰고,
  별도 릴레이가 읽어 보낸다. 따라서 업무 변경이 영속이면 outbox 행도 영속이다(둘 사이에 크래시 창이 없다).
- 계약 I6(boundary-contracts INTERFACES.md): Trace 수집은 최소 1회 전달이며 수집 장애·지연이 턴을 막지 않는다.
시험은 구현 구조를 모른다: 세션 commit이 영속된 직후 프로세스가 죽는 것(commit이 돌아온 뒤 BaseException)을 주입하고,
'재시작' 뒤 새로 연 저장소들에서 불변식 "확정된 턴마다 outbox 행이 있다"를 본다.
"""
import pytest

from .timing import FakeWait, fake_clock
from survey_service.wiring.db import open_stores
from survey_service.settings import Settings
from survey_service.components.trace_outbox import Outbox
from survey_service.components.trace_envelope import OutboxTraceSink
from survey_service.components.link_trace_export import TraceExporter

from .e2e_support import ANSWER_OUT, Rig
from .fakes import FakeTraceIngest
from .pg_support import pg_url


class ProcessCrash(BaseException):
    """프로세스 사망 모사: 예외 처리기가 삼키지 못하는 BaseException."""


def _stores(request, tmp_path):
    kind = request.param
    s = Settings(session_db=str(tmp_path / "s.db"), outbox_db=str(tmp_path / "o.db"), alert_db=str(tmp_path / "a.db"))
    return (lambda: open_stores(s, url="")) if kind == "sqlite" else (lambda u=pg_url(request): open_stores(s, url=u))


@pytest.fixture(params=["sqlite", "postgres"])
def opener(request, tmp_path):
    return _stores(request, tmp_path)


def _raw_rows(opener):
    """재시작한 프로세스가 보는 것: (확정된 턴 키 집합, outbox 행 키 집합). 확정 여부는 저장소의 공개 읽기(get_turn)로, outbox 행은 표를 직접 읽는다."""
    store, outbox, alerts, kind = opener()
    try:
        committed = set()
        for sid in ("S1",):
            for tid in ("t1", "t2", "t3"):
                rec = store.get_turn(sid, tid)
                if rec is not None and rec.committed:
                    committed.add((sid, tid))
        keys = {(r[0], r[1]) for r in outbox._db.execute("SELECT session_id, turn_id FROM outbox").fetchall()}
        return committed, keys
    finally:
        store.close(); outbox.close(); alerts.close()


def test_crash_right_after_session_commit_does_not_lose_the_trace(opener):
    store, outbox, alerts, kind = opener()
    real_commit = store.commit

    def commit_then_die(request, *, turn_id, **kw):
        real_commit(request, turn_id=turn_id, **kw)      # 세션 변경이 영속이 된다
        raise ProcessCrash()                             # 그 직후 프로세스가 죽는다
    rig = Rig(store=store, trace_sink=OutboxTraceSink(outbox, clock=fake_clock()))
    rig.say(kind="SESSION_START")                        # t1: 정상 턴(commit 정상)
    store.commit = commit_then_die
    with pytest.raises(ProcessCrash):
        rig.say("안 피움", out=ANSWER_OUT)               # t2: commit 직후 사망
    store.close(); outbox.close(); alerts.close()

    committed, keys = _raw_rows(opener)
    assert ("S1", "t2") in committed, "시험 전제: t2의 세션 변경은 영속이다"
    missing = committed - keys
    assert not missing, f"세션 변경은 영속인데 outbox 행이 없다(Trace 유실): {sorted(missing)}"


def test_row_left_by_a_crash_is_eventually_visible_not_stuck(opener):
    """크래시로 남은 행은 영원히 보내지 않는 상태로 남지 않는다: 유예 뒤 보내거나(SENT) 보이게 격리한다(DEAD)."""
    store, outbox, alerts, kind = opener()
    real_commit = store.commit

    def commit_then_die(request, *, turn_id, **kw):
        real_commit(request, turn_id=turn_id, **kw)
        raise ProcessCrash()
    rig = Rig(store=store, trace_sink=OutboxTraceSink(outbox, clock=fake_clock()))
    rig.say(kind="SESSION_START")
    store.commit = commit_then_die
    with pytest.raises(ProcessCrash):
        rig.say("안 피움", out=ANSWER_OUT)
    store.close(); outbox.close(); alerts.close()

    store, outbox, alerts, kind = opener()
    try:
        clock = [10_000_000_000.0]                       # 한참 뒤(유예 경과)
        ex = TraceExporter(outbox, FakeTraceIngest(), clock=lambda: clock[0], wait=FakeWait())
        for _ in range(3):
            ex.flush_once()
        c = outbox.counts()
        assert c["PENDING"] == 0 and c.get("STAGED", 0) == 0, c
        assert c["SENT"] + c["DEAD"] >= 2, c             # 정상 턴(t1)과 크래시 턴(t2) 모두 처리됐다
    finally:
        store.close(); outbox.close(); alerts.close()
