"""재알림과 직원 해결의 경합(CR-H001): 서로 다른 DB 연결 두 개(프로세스 안 `threading.Lock`이 못 막는 경우)에서도
`begin_retry`의 `AND status='OPEN'` 조건이 해결된 알림에 'alert' 이벤트가 나가지 않게 막는지 본다. 고유 이름의 일회용 PostgreSQL 컨테이너를 쓴다."""
import threading

import pytest

from survey_service.components.alert_book import PgAlertStore
from survey_service.wiring.errors import StaffCallRejected

from .timing import HANG_GUARD_SECONDS, fake_clock
from . import pg_container
from .pg_support import pg_link

pytestmark = pytest.mark.skipif(not pg_container.docker_ready(), reason="Docker 또는 postgres 이미지가 없어 건너뜀")


@pytest.fixture(scope="module")
def url():
    name, u = pg_container.start_postgres()
    yield u
    pg_container.stop(name)


def test_retry_after_resolve_on_another_connection_is_rejected(url):
    a, b = PgAlertStore(url, **pg_link(), clock=fake_clock()), PgAlertStore(url, **pg_link(), clock=fake_clock())              # 연결이 서로 다르다: 한 프로세스 락을 공유하지 않는다
    try:
        a.create("S-seq", "RISK")
        row = a._db.execute("SELECT n FROM alerts WHERE session_id=?", ("S-seq",)).fetchone()[0]
        assert a is not b and a._db is not b._db
        b.resolve("S-seq", "RESUME")
        with pytest.raises(StaffCallRejected):
            a.begin_retry(row, "t-1")
        evs = a.events_after(0)
        resolved = max(e["id"] for e in evs if e["type"] == "resolved" and e["alert"]["session_id"] == "S-seq")
        assert not [e for e in evs if e["type"] == "alert" and e["alert"]["session_id"] == "S-seq" and e["id"] > resolved]
    finally:
        a.close()
        b.close()


def test_concurrent_retry_and_resolve_never_emit_alert_event_after_resolved(url):
    a, b = PgAlertStore(url, **pg_link(), clock=fake_clock()), PgAlertStore(url, **pg_link(), clock=fake_clock())
    try:
        sessions = [f"S-race-{i}" for i in range(40)]
        ns = {}
        for s in sessions:
            a.create(s, "RISK")
            ns[s] = a._db.execute("SELECT n FROM alerts WHERE session_id=?", (s,)).fetchone()[0]
        outcome = {}
        for s in sessions:
            gate = threading.Barrier(2)

            def retry(s=s):
                gate.wait()
                try:
                    a.begin_retry(ns[s], "t-" + s)
                    outcome[s] = "RETRIED"
                except StaffCallRejected:
                    outcome[s] = "REJECTED"

            def resolve(s=s):
                gate.wait()
                b.resolve(s, "RESUME")
            ts = [threading.Thread(target=retry), threading.Thread(target=resolve)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(HANG_GUARD_SECONDS)
        evs = a.events_after(0, limit=100000)
        resolved_id = {e["alert"]["alert_id"]: e["id"] for e in evs if e["type"] == "resolved" and e["alert"]["session_id"] in ns}
        assert len(resolved_id) == len(sessions)
        for s in sessions:
            aid = [e["alert"]["alert_id"] for e in evs if e["type"] == "resolved" and e["alert"]["session_id"] == s][0]
            after = [e for e in evs if e["type"] == "alert" and e["alert"]["alert_id"] == aid and e["id"] > resolved_id[aid]]
            assert not after, (s, outcome.get(s))              # 해결된 알림에 재알림 이벤트가 나가지 않았다
        assert set(outcome.values()) <= {"RETRIED", "REJECTED"} and len(outcome) == len(sessions)
    finally:
        a.close()
        b.close()
