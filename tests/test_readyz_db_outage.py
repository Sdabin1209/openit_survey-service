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


def _container_for(url: str):
    """TEST_DATABASE_URL의 호스트 포트를 게시한 컨테이너 이름(없으면 None)."""
    import re
    import subprocess
    m = re.search(r":(\d+)/", url)
    try:
        for cid in subprocess.run(["docker", "ps", "-q", "--filter", "label=io.openit.dev-postgres=1"], capture_output=True, text=True, timeout=20).stdout.split():
            ports = subprocess.run(["docker", "port", cid, "5432/tcp"], capture_output=True, text=True, timeout=20).stdout
            if m and f":{m.group(1)}" in ports:
                return cid
    except Exception:
        return None
    return None


def test_turn_ends_not_committed_within_deadline_when_db_is_frozen(request):
    """docker pause 같은 무응답 DB: 서버 statement_timeout은 못 듣는다. 클라이언트 강제 끊기와 턴 총 마감으로 NOT_COMMITTED로 끝나고, 풀리면 복구된다."""
    import subprocess
    from survey_service import pg
    from survey_service.store.postgres import PgSessionStore
    from .e2e_support import ANSWER_OUT, Rig
    url = pg_url(request)
    import os
    cid = _container_for(os.environ["TEST_DATABASE_URL"])
    if not cid:
        pytest.skip("docker로 PostgreSQL 컨테이너를 멈출 수 없음")
    pg.configure(connect_timeout=1, statement_timeout=1)
    request.addfinalizer(lambda: pg.configure(connect_timeout=pg.DEFAULT_CONNECT_TIMEOUT, statement_timeout=pg.DEFAULT_STATEMENT_TIMEOUT))
    store = PgSessionStore(url)
    request.addfinalizer(store.close)
    rig = Rig(store=store, turn_deadline_seconds=4)
    rig.start()
    before = rig.doc()["answer_records"][0]["current_value"]
    rig.model.queue.append(ANSWER_OUT)
    subprocess.check_call(["docker", "pause", cid], stdout=subprocess.DEVNULL)
    try:
        t = time.monotonic()
        out = rig.say("안 피움", turn_id="t-frozen")
        dt = time.monotonic() - t
        assert dt < 6 and out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED", dt
    finally:
        subprocess.call(["docker", "unpause", cid], stdout=subprocess.DEVNULL)
    time.sleep(1)
    assert rig.doc()["answer_records"][0]["current_value"] == before          # 저장되지 않았다
    rig.model.queue.append(ANSWER_OUT)
    out = rig.say("안 피움", turn_id="t-frozen")                                 # 풀린 뒤 같은 turn_id 재전송은 처리된다
    assert out.frontend_input["turn_failure_signal"] is None
