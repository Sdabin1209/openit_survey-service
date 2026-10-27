"""PostgreSQL 저장소: 알림 DB·outbox의 적합성(SQLite와 같은 시험), 끊긴 연결 재접속, 접속 문자열 비노출, 데이터베이스 종류. TEST_DATABASE_URL이 없으면 SKIP."""
import json
import logging

import pytest

from survey_service.alerts import AlertStore, PgAlertStore
from survey_service.errors import StoreError
from survey_service.pg import translate
from survey_service.store.postgres import PgSessionStore
from survey_service.trace.outbox import Outbox
from survey_service.trace.pg_outbox import PgOutbox

from .pg_support import kill_connection, pg_url


@pytest.fixture(params=["sqlite", "postgres"])
def alerts(request, tmp_path):
    t = [1_700_000_000.0]
    if request.param == "postgres":
        s = PgAlertStore(pg_url(request), clock=lambda: t[0])
    else:
        s = AlertStore(str(tmp_path / "a.db"), clock=lambda: t[0])
    s.t = t
    yield s
    s.close()


@pytest.fixture(params=["sqlite", "postgres"])
def outbox(request, tmp_path):
    ob = PgOutbox(pg_url(request)) if request.param == "postgres" else Outbox(str(tmp_path / "o.db"))
    yield ob
    ob.close()


def env_(sid, tid):
    return {"session_id": sid, "turn_id": tid, "x": "한글"}


def test_translate_placeholders_and_percent():
    assert translate("SELECT ? WHERE a LIKE 'E%' AND b=?") == "SELECT %s WHERE a LIKE 'E%%' AND b=%s"
    assert translate("BEGIN IMMEDIATE") == "BEGIN"


def test_alert_store_lifecycle(alerts):
    ev = alerts.create("S1", "RISK_STOP", "internal:r1")
    assert ev["alert"]["alert_id"] == "al-1" and ev["id"] == 1 and ev["alert"]["status"] == "OPEN"
    alerts.create("S2", "CALL_STAFF")
    items, nxt = alerts.list("OPEN", 10, None)
    assert [a["session_id"] for a in items] == ["S2", "S1"] and nxt is None
    page, nxt = alerts.list(None, 1, None)
    assert [a["session_id"] for a in page] == ["S2"] and nxt == "2"
    assert [a["session_id"] for a in alerts.list(None, 1, nxt)[0]] == ["S1"]
    ev2 = alerts.resolve("S1", "RESUME", "dr.kim")
    assert [e["type"] for e in ev2] == ["resolved"] and alerts.resolve("S1", "RESUME") == []
    rows = alerts.alerts_of_session("S1")
    assert rows == [{"alert_id": "al-1", "kind": "RISK_STOP", "status": "RESOLVED", "created_at": rows[0]["created_at"], "rule_id": "internal:r1",
                     "resolved_by": "dr.kim"}]
    assert [e["id"] for e in alerts.events_after(0)] == [1, 2, 3] and alerts.latest_event_id() == 3
    assert alerts.trim_events(keep=1) == 2 and [e["id"] for e in alerts.events_after(0)] == [3]


def test_alert_store_push_meta_timeline_signals_access_purge(alerts):
    sub = {"endpoint": "https://p.example/1", "keys": {"p256dh": "a", "auth": "b"}}
    assert alerts.upsert_subscription(sub) is True and alerts.upsert_subscription(sub) is False
    assert alerts.count_subscriptions() == 1 and alerts.subscriptions() == [sub]
    assert alerts.remove_subscription(sub["endpoint"]) is True and alerts.remove_subscription(sub["endpoint"]) is False
    alerts.set_subject("S1", "PT-1"); alerts.set_subject("S1", "OTHER"); alerts.set_subject("S2", None)
    assert alerts.subject_of("S1") == "PT-1" and alerts.subject_of("S2") is None and alerts.subject_of("S3") is None
    assert alerts.sessions_of_subject("PT-1") == ["S1"] and alerts.subjects_of(["S1", "S2", "S9"]) == {"S1": "PT-1", "S2": None}
    alerts.record("S1", -1, "start"); alerts.record("S1", 0, "t-1"); alerts.t[0] += 5; alerts.record("S1", 0, "late")
    tl = alerts.timeline("S1")
    assert tl[0]["turn_id"] == "t-1" and set(tl) == {-1, 0}
    sig = {"signal_id": "g1", "source": "a", "code": "c"}
    assert alerts.get_signal("S1", "g1") is None
    alerts.record_signal("S1", sig, 7, "PAUSED_RISK"); alerts.record_signal("S1", sig, 8, "PAUSED_RISK")
    assert alerts.get_signal("S1", "g1") == {"alert_id": "al-7", "session_status": "PAUSED_RISK"}
    alerts.log_access("staff", "S1", "OK"); alerts.log_access_many("result-reader", ["S1", "S2"], "OK", "list status")
    log = alerts.access_log()
    assert [(r["role"], r["session_id"]) for r in reversed(log)] == [("staff", "S1"), ("result-reader", "S1"), ("result-reader", "S2")]
    assert alerts.purge_sessions(["S1"]) >= 4
    assert alerts.subject_of("S1") is None and alerts.timeline("S1") == {} and alerts.get_signal("S1", "g1") is None
    assert len(alerts.access_log("S1")) == 2                    # 접근 기록은 지우지 않는다


def test_outbox_conformance(outbox):
    assert outbox.enqueue(env_("S", "t1"), 10.0) is True and outbox.enqueue(env_("S", "t1"), 11.0) is False
    outbox.enqueue(env_("S", "t2"), 12.0)
    assert [r.turn_id for r in outbox.due(20.0)] == ["t1", "t2"] and outbox.due(20.0)[0].envelope["x"] == "한글"
    r1, r2 = outbox.due(20.0)
    outbox.mark_failed(r1.id, 100.0, "boom")
    assert [r.turn_id for r in outbox.due(50.0)] == ["t2"] and outbox.due(50.0)[0].attempts == 0
    outbox.mark_sent(r2.id, 30.0)
    outbox.mark_dead(r1.id, 31.0, "dead")
    assert outbox.counts() == {"PENDING": 0, "SENT": 1, "DEAD": 1}
    assert outbox.dead_rows() == [{"session_id": "S", "turn_id": "t1", "error": "dead"}]
    assert outbox.oldest_pending_age(40.0) is None
    outbox.enqueue(env_("S", "t3"), 35.0)
    assert outbox.oldest_pending_age(40.0) == 5.0
    assert outbox.purge_sent(100.0) == 1 and outbox.counts()["SENT"] == 0
    outbox.ping()


# ---------------------------------------------------------------- PostgreSQL 전용
def test_session_store_reconnects_after_connection_kill(request):
    from .test_store import make, writes
    s = PgSessionStore(pg_url(request))
    st, rev = make(s)
    kill_connection(s._db)
    assert s.load("S1")["status"] == "OK"                       # 끊긴 뒤 첫 읽기가 새 연결로 복구한다
    kill_connection(s._db)
    assert s.release_id_of("S1") == "rel-1"
    kill_connection(s._db)
    s.ping()
    kill_connection(s._db)
    res = s.commit({"session_id": "S1", "expected_revision": rev, "writes": writes(5)}, turn_id="t1", input_hash="h")
    assert res["status"] == "OK" and res["write_outcome"] == "APPLIED"      # 트랜잭션 시작 전에 연결을 다시 연다
    s.close()


def test_alert_store_and_outbox_reconnect(request):
    url = pg_url(request)
    a, o = PgAlertStore(url), PgOutbox(url)
    a.create("S1", "RISK_STOP")
    kill_connection(a._db)
    assert a.list(None, 10, None)[0][0]["session_id"] == "S1"
    kill_connection(a._db)
    assert a.create("S2", "CALL_STAFF")["alert"]["session_id"] == "S2"
    o.enqueue(env_("S", "t1"), 1.0)
    kill_connection(o._db)
    assert o.counts()["PENDING"] == 1
    kill_connection(o._db)
    o.mark_sent(o.due(5.0)[0].id, 6.0)
    assert o.counts()["SENT"] == 1
    a.close(); o.close()


def test_connection_lost_inside_transaction_is_not_retried(request):
    s = PgSessionStore(pg_url(request))
    s._db.execute("BEGIN")
    kill_connection(s._db)
    with pytest.raises(Exception):
        s._db.execute("SELECT 1")                               # 트랜잭션 안에서 끊기면 조용히 새 연결로 이어 가지 않는다
    s._db.execute("ROLLBACK")
    s.ping()
    s.close()


def test_unrecoverable_is_store_error_and_hides_url(request, caplog):
    s = PgSessionStore(pg_url(request))
    s._db._url = "postgresql://svc:pw-canary-77@127.0.0.1:1/db"       # 다시 열 수 없는 주소
    kill_connection(s._db)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(StoreError) as e:
            s.ping()
        r = s.load("S1")
    assert "pw-canary-77" not in str(e.value) and "127.0.0.1" not in str(e.value) and r["status"] == "DB_ERROR"
    assert "pw-canary-77" not in caplog.text


def test_bad_url_is_store_error_without_secret():
    with pytest.raises(StoreError) as e:
        PgSessionStore("postgresql://svc:pw-canary-88@127.0.0.1:1/db")
    assert "pw-canary-88" not in str(e.value) and "127.0.0.1" not in str(e.value)


def test_app_with_database_url_reports_kind_and_never_exposes_url(request, tmp_path, monkeypatch):
    from survey_service.service import App
    from survey_service.settings import Settings
    from .fakes import FakeReleaseReader, FakeTraceIngest
    from .sample_bank import sample_bank
    from .test_release_loader import INSTALLED
    url = pg_url(request)
    monkeypatch.setenv("SURVEY_DATABASE_URL", url)
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", sample_bank(), turn_engine_version=INSTALLED["turn_engine_version"], model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    env = {"SURVEY_WORK_DIR": str(tmp_path / "w"), "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake",
           "SURVEY_SESSION_IDLE_SECONDS": "0", "SURVEY_SESSION_DB": str(tmp_path / "never.db")}
    app = App(Settings.from_env(env), reader=reader, ingest=FakeTraceIngest())
    try:
        app.start()
        ok, body = app.readiness()
        assert ok and body["database"] == {"kind": "postgresql"} and type(app.store).__name__ == "PgSessionStore"
        text = json.dumps(body)
        assert url not in text and url.split("@")[0].split(":")[-1] not in text
        assert not (tmp_path / "never.db").exists()
        st, _, _ = app.api.handle("POST", "/v1/sessions", "Bearer test-api-token", json.dumps({"session_id": "K1", "kind": "SESSION_START"}).encode())
        assert st == 200
        kill_connection(app.store._db)                               # 서비스 재시작 없이 복구
        assert app.api.handle("GET", "/v1/sessions/K1/state", "Bearer test-api-token", b"")[0] == 200
        kill_connection(app.alert_store._db)
        assert app.readiness()[0] is True
    finally:
        app.stop()


def test_app_default_is_sqlite_kind(tmp_path):
    from survey_service.service import App
    from survey_service.settings import Settings
    from .fakes import FakeReleaseReader, FakeTraceIngest
    env = {"SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"), "SURVEY_WORK_DIR": str(tmp_path / "w"),
           "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake", "SURVEY_SESSION_IDLE_SECONDS": "0"}
    app = App(Settings.from_env(env), reader=FakeReleaseReader(tmp_path / "g"), ingest=FakeTraceIngest())
    try:
        assert app.readiness()[1]["database"] == {"kind": "sqlite"}
    finally:
        app.stop()
