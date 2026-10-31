"""/readyz는 DB가 죽거나 멈춰도 짧은 상한 안에 503(db unavailable)으로 답하고, 복구되면 다시 200이 된다(CR-D23).
PostgreSQL 시험은 TEST_DATABASE_URL이 없으면 SKIP(통과로 세지 않는다). 연결 불가는 응답 없는 주소(10.255.255.1)로 만든다."""
from .timing import HANG_GUARD_SECONDS, POLL_SECONDS, FakeClock, wait_until
import json
import threading
import time

import pytest

from survey_service.wiring.app import App
from survey_service.settings import Settings

from .http_server_support import start_server, server_port, stop_server, listen_backlog
from .fakes import FakeReleaseReader, FakeTraceIngest
from .pg_support import pg_url
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED

UNREACHABLE = "postgresql://u:p-canary-77@127.0.0.1:1/d"      # 닫힌 포트: 연결 거부가 즉시 난다(응답 없는 주소로 라이브러리의 접속 시한을 기다리지 않는다)


def _app(tmp_path, monkeypatch, url=None, extra=None, clock=None):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", sample_bank(), action_rules_version=INSTALLED["action_rules_version"], model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    env = {"SURVEY_WORK_DIR": str(tmp_path / "w"), "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake",
           "SURVEY_SESSION_IDLE_SECONDS": "0", "SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db")}
    env.update(extra or {})
    if url:
        monkeypatch.setenv("SURVEY_DATABASE_URL", url)
    app = App(Settings.from_env(env), reader=reader, ingest=FakeTraceIngest(), **({"clock": clock} if clock else {}))
    app.start()
    return app


def _ready(app):
    ok, body = app.readiness()
    return ok, body


def hanging(gate, entered):
    """영영 끝나지 않는 확인(gate를 set하기 전까지). 시작했음을 entered로 알린다."""
    def f(*a, **k):
        entered.set()
        gate.wait(HANG_GUARD_SECONDS)
    return f


def ready_after_deadline(app, clock, entered):
    """readiness를 다른 스레드에서 시작하고, 막힌 확인이 모두 시작된 뒤 주입한 시계로 마감을 넘긴다(실제로 기다리지 않는다). 돌아온 (ok, body)."""
    res = {}
    th = threading.Thread(target=lambda: res.update(r=app.readiness()), daemon=True)
    th.start()
    for ev in entered:
        assert ev.wait(HANG_GUARD_SECONDS)
    # 마감 시각은 readiness 스레드가 확인 시작 뒤에 정하므로(경쟁) 돌아올 때까지 시계를 계속 흘린다: 시계는 시험이 흘릴 때만 가고 실제 시간은 판정에 쓰이지 않는다
    for _ in range(HANG_GUARD_SECONDS * 20):
        clock.advance(app.settings.readyz_db_timeout_seconds + 10)
        th.join(POLL_SECONDS)
        if not th.is_alive():
            break
    assert not th.is_alive()
    return res["r"]


def _break(app):
    saved = []
    for st in (app.store, app.outbox, app.alert_store):
        saved.append((st._db, st._db._url))
        st._db._conn.close()
        st._db._url = UNREACHABLE
    return saved


def _heal(saved):
    for db, url in saved:
        db._url = url


def test_readyz_503_when_db_unreachable_then_recovers(request, tmp_path, monkeypatch):
    """연결 거부(즉시 실패하는 가짜 장애)에서 /readyz가 503 db unavailable을 사유와 함께 내고, 연결이 돌아오면 다시 200이 된다. 시간은 재지 않는다(접속 시한은 라이브러리의 몫)."""
    app = _app(tmp_path, monkeypatch, url=pg_url(request))
    try:
        assert _ready(app)[0] is True
        saved = _break(app)
        for _ in range(2):
            ok, body = _ready(app)
            assert ok is False
            assert {"session_db", "outbox_db", "alert_db"} <= set(body["problems"])
            assert body["database"]["status"] == "UNAVAILABLE" and body["database"]["detail"] == "db unavailable"
            assert body["db_stats_stale"] is True
            assert "p-canary-77" not in json.dumps(body) and "127.0.0.1:1" not in json.dumps(body)
        _heal(saved)
        ok, body = _ready(app)
        assert ok is True and body["database"] == {"kind": "postgresql"}      # 준비 판단은 주 연결과 무관하게 바로 회복한다
        # 보조 수치는 주 연결이 되살아나면 다시 신선해진다. 기다림은 조건(stale이 풀림)으로 하고 20초는 매달림 감시다(정상은 재접속 유예 1초 안팎).
        assert wait_until(lambda: _ready(app)[1]["db_stats_stale"] is False)
        assert _ready(app)[0] is True
    finally:
        app.stop()


def test_readyz_does_not_wait_for_write_path_lock(request, tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, url=pg_url(request))
    held, release = threading.Event(), threading.Event()

    def hold():
        with app.store._db._lock, app.outbox._db._lock:      # 요청 경로·전송 스레드가 연결을 쥐고 기다리는 상황
            held.set()
            release.wait(HANG_GUARD_SECONDS)          # 매달림 감시: 시험이 끝나면 finally에서 풀린다
    th = threading.Thread(target=hold, daemon=True)
    th.start()
    try:
        assert held.wait(HANG_GUARD_SECONDS)                       # 매달림 감시
        ok, body = _ready(app)                      # 락을 쥔 채로 돌아온다: 락을 기다렸다면 release 전에는 돌아오지 못한다(기다렸다면 아래 th.is_alive()가 거짓이 된다)
        assert th.is_alive(), "readyz가 쓰기 경로 락이 풀릴 때까지 기다렸다"
        assert "session_db" not in body["problems"] and "outbox_db" not in body["problems"]
    finally:
        release.set()
        th.join(HANG_GUARD_SECONDS)
        app.stop()


def test_readyz_has_hard_cap_when_a_check_hangs(tmp_path, monkeypatch):
    """확인 하나가 영영 안 끝나도(드라이버가 멈춘 경우) 마감(주입한 시계로 흘린다) 뒤에 503을 낸다. SQLite 경로의 보통 동작은 그대로다."""
    clock = FakeClock()
    app = _app(tmp_path, monkeypatch, extra={"SURVEY_READYZ_DB_TIMEOUT_SECONDS": "1"}, clock=clock)
    gate, entered = threading.Event(), threading.Event()
    try:
        ok, body = _ready(app)
        assert ok is True and body["database"] == {"kind": "sqlite"} and body["db_stats_stale"] is False
        app.outbox.ping = hanging(gate, entered)
        ok, body = ready_after_deadline(app, clock, [entered])
        assert ok is False and body["problems"] == ["outbox_db"]
    finally:
        gate.set()
        app.stop()


def test_readyz_db_timeout_setting():
    base = {"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake"}
    assert Settings.from_env(base).readyz_db_timeout_seconds == 2.0
    assert Settings.from_env({**base, "SURVEY_READYZ_DB_TIMEOUT_SECONDS": "3"}).readyz_db_timeout_seconds == 3.0
    with pytest.raises(Exception):
        Settings.from_env({**base, "SURVEY_READYZ_DB_TIMEOUT_SECONDS": "0"})


def test_db_timeouts_are_passed_from_settings_to_the_connection(monkeypatch):
    """PostgreSQL 접속·문장 시한을 실제로 자르는 것은 libpq·서버의 몫이다(라이브러리). 이 영역의 책임은 설정 값을 연결에 넘기는 것이다: 시간을 재지 않고 넘긴 값을 확인한다."""
    from survey_service.components import session_store
    from survey_service.wiring import db
    seen = {}
    monkeypatch.setattr(session_store, "configure", lambda **kw: seen.update(kw))
    monkeypatch.setattr(db, "PgSessionStore", lambda url, *a, **k: type("S", (), {})())
    monkeypatch.setattr(db, "PgOutbox", lambda url, **k: type("O", (), {})())
    monkeypatch.setattr(db, "PgAlertStore", lambda url, **k: type("A", (), {})())
    s = Settings.from_env({"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake",
                           "SURVEY_DB_CONNECT_TIMEOUT_SECONDS": "3", "SURVEY_DB_STATEMENT_TIMEOUT_SECONDS": "4", "SURVEY_READYZ_DB_TIMEOUT_SECONDS": "5"})
    db.open_stores(s, url="postgresql://u:p@127.0.0.1:1/d")
    assert seen["connect_timeout"] == 3 and seen["statement_timeout"] == 4 and seen["probe_timeout"] == 5 and seen["reconnect_backoff"] == s.db_reconnect_backoff_seconds


def test_readyz_reports_shutting_down_while_draining(tmp_path, monkeypatch):
    """N8: SIGTERM을 받으면 draining을 켠다(main과 같다). 그동안 /readyz는 503 shutting_down이다. 처리 중인 요청을 끝까지 기다리는 쪽(waitress 닫기)은 라이브러리이고,
    우리 close_server의 기다림은 tests/test_http_server_properties.py가 사건(막힌 요청을 Event로 붙잡음)으로 본다."""
    app = _app(tmp_path, monkeypatch)
    try:
        assert app.readiness()[0] is True
        app.draining.set()
        ok, body = app.readiness()
        assert ok is False and "shutting_down" in body["problems"]
    finally:
        app.draining.set(); app.stop()


def test_readyz_total_deadline_with_three_hanging_dbs_and_reasons_are_kept(tmp_path, monkeypatch):
    """FAULT_POLICY 5: 의존이 응답하지 않아도 /readyz는 마감(주입한 시계) 뒤 사유와 함께 돌아온다.
    확인 셋이 모두 멈추면 사유 셋이 모두 나오고, 하나만 멈추거나 실패하면 그 사유만 나온다. 실제 시간은 기다리지 않는다."""
    clock = FakeClock()
    app = _app(tmp_path, monkeypatch, clock=clock)
    gate = threading.Event()
    try:
        ents = [threading.Event() for _ in range(3)]
        monkeypatch.setattr(app.store, "ping", hanging(gate, ents[0]))
        monkeypatch.setattr(app.outbox, "ping", hanging(gate, ents[1]))
        monkeypatch.setattr(app.alert_store, "ping", hanging(gate, ents[2]))
        ok, body = ready_after_deadline(app, clock, ents)
        assert ok is False
        assert {"session_db", "outbox_db", "alert_db"} <= set(body["problems"])

        def boom():
            raise RuntimeError("x")
        monkeypatch.setattr(app.store, "ping", boom)                  # 하나는 즉시 실패, 하나는 멈춤, 하나는 정상
        monkeypatch.setattr(app.alert_store, "ping", lambda: None)
        ents2 = threading.Event()
        monkeypatch.setattr(app.outbox, "ping", hanging(gate, ents2))
        ok, body = ready_after_deadline(app, clock, [ents2])
        assert ok is False
        assert [p for p in body["problems"] if p.endswith("_db")] == ["session_db", "outbox_db"]
    finally:
        gate.set()
        app.stop()
