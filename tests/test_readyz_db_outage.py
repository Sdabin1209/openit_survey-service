"""/readyz는 DB가 죽거나 멈춰도 짧은 상한 안에 503(db unavailable)으로 답하고, 복구되면 다시 200이 된다(CR-D23).
PostgreSQL 시험은 TEST_DATABASE_URL이 없으면 SKIP(통과로 세지 않는다). 연결 불가는 응답 없는 주소(10.255.255.1)로 만든다."""
import json
import threading
import time

import pytest

from survey_service.service import App
from survey_service.settings import Settings

from .fakes import FakeReleaseReader, FakeTraceIngest
from .pg_support import pg_url
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED

BLACKHOLE = "postgresql://u:p-canary-77@10.255.255.1:5432/d"
LIMIT = 6.0        # 기본 2초 상한 + 동시 확인 여유 1초 + 보조 수치 1초 + 여유. 장애 전의 관찰값 37초와 비교한다


def _app(tmp_path, monkeypatch, url=None, extra=None):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", sample_bank(), turn_engine_version=INSTALLED["turn_engine_version"], model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    env = {"SURVEY_WORK_DIR": str(tmp_path / "w"), "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake",
           "SURVEY_SESSION_IDLE_SECONDS": "0", "SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db")}
    env.update(extra or {})
    if url:
        monkeypatch.setenv("SURVEY_DATABASE_URL", url)
    app = App(Settings.from_env(env), reader=reader, ingest=FakeTraceIngest())
    app.start()
    return app


def _timed(app):
    t = time.monotonic()
    ok, body = app.readiness()
    return ok, body, time.monotonic() - t


def _break(app):
    saved = []
    for st in (app.store, app.outbox, app.alert_store):
        saved.append((st._db, st._db._url))
        st._db._conn.close()
        st._db._url = BLACKHOLE
    return saved


def _heal(saved):
    for db, url in saved:
        db._url = url


def test_readyz_503_within_limit_when_db_unreachable_then_recovers(request, tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, url=pg_url(request))
    try:
        assert _timed(app)[0] is True
        saved = _break(app)
        for _ in range(2):
            ok, body, dt = _timed(app)
            assert ok is False and dt < LIMIT, dt
            assert {"session_db", "outbox_db", "alert_db"} <= set(body["problems"])
            assert body["database"]["status"] == "UNAVAILABLE" and body["database"]["detail"] == "db unavailable"
            assert body["db_stats_stale"] is True
            assert "p-canary-77" not in json.dumps(body) and "10.255.255.1" not in json.dumps(body)
        _heal(saved)
        ok, body, dt = _timed(app)
        assert ok is True and body["database"] == {"kind": "postgresql"} and dt < LIMIT      # 준비 판단은 주 연결과 무관하게 바로 회복한다
        end = time.monotonic() + 20          # 보조 수치는 주 연결이 되살아나면(남은 접속 시도가 끝나면) 다시 신선해진다
        while body["db_stats_stale"] and time.monotonic() < end:
            time.sleep(0.5)
            ok, body, _ = _timed(app)
        assert ok is True and body["db_stats_stale"] is False
    finally:
        app.stop()


def test_readyz_does_not_wait_for_write_path_lock(request, tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, url=pg_url(request))
    held, release = threading.Event(), threading.Event()

    def hold():
        with app.store._db._lock, app.outbox._db._lock:      # 요청 경로·전송 스레드가 연결을 쥐고 기다리는 상황
            held.set()
            release.wait(20)
    th = threading.Thread(target=hold, daemon=True)
    th.start()
    try:
        assert held.wait(5)
        ok, body, dt = _timed(app)
        assert ok is False or ok is True          # 응답 자체가 핵심이다
        assert dt < LIMIT, dt
        assert "session_db" not in body["problems"] and "outbox_db" not in body["problems"]
    finally:
        release.set()
        th.join(5)
        app.stop()


def test_readyz_has_hard_cap_when_a_check_hangs(tmp_path, monkeypatch):
    """확인 하나가 영영 안 끝나도(드라이버가 멈춘 경우) 상한 뒤에 503을 낸다. SQLite 경로의 보통 동작은 그대로다."""
    app = _app(tmp_path, monkeypatch, extra={"SURVEY_READYZ_DB_TIMEOUT_SECONDS": "1"})
    stop = threading.Event()
    try:
        ok, body, _ = _timed(app)
        assert ok is True and body["database"] == {"kind": "sqlite"} and body["db_stats_stale"] is False
        app.outbox.ping = lambda: stop.wait(60)
        ok, body, dt = _timed(app)
        assert ok is False and body["problems"] == ["outbox_db"] and dt < 3.5, dt
    finally:
        stop.set()
        app.stop()


def test_readyz_db_timeout_setting():
    base = {"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake"}
    assert Settings.from_env(base).readyz_db_timeout_seconds == 2.0
    assert Settings.from_env({**base, "SURVEY_READYZ_DB_TIMEOUT_SECONDS": "3"}).readyz_db_timeout_seconds == 3.0
    with pytest.raises(Exception):
        Settings.from_env({**base, "SURVEY_READYZ_DB_TIMEOUT_SECONDS": "0"})
