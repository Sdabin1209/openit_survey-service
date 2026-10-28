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


def test_turn_ends_not_committed_within_deadline_when_db_is_frozen(request):
    """docker pause 같은 무응답 DB: 서버 statement_timeout은 못 듣는다. 클라이언트 강제 끊기와 턴 총 마감으로 NOT_COMMITTED로 끝나고, 풀리면 복구된다."""
    from survey_service import pg
    from survey_service.store.postgres import PgSessionStore
    from .e2e_support import ANSWER_OUT, Rig
    from . import pg_container as pc
    if not pc.docker_ready():
        pytest.skip("docker 또는 postgres:16 이미지 없음: 일회용 PostgreSQL을 띄울 수 없음")
    cid, url = pc.start_postgres()          # 이 시험이 띄운 컨테이너만 이름으로 멈춘다(남의 dev-postgres에 닿지 않게)
    request.addfinalizer(lambda: pc.stop(cid))
    pg.configure(connect_timeout=1, statement_timeout=1)
    request.addfinalizer(lambda: pg.configure(connect_timeout=pg.DEFAULT_CONNECT_TIMEOUT, statement_timeout=pg.DEFAULT_STATEMENT_TIMEOUT))
    store = PgSessionStore(url)
    request.addfinalizer(store.close)
    rig = Rig(store=store, turn_deadline_seconds=4)
    rig.start()
    before = rig.doc()["answer_records"][0]["current_value"]
    rig.model.queue.append(ANSWER_OUT)
    pc.pause(cid)
    try:
        t = time.monotonic()
        out = rig.say("안 피움", turn_id="t-frozen")
        dt = time.monotonic() - t
        assert dt < 6 and out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED", dt
    finally:
        pc.unpause(cid)
    time.sleep(1)
    assert rig.doc()["answer_records"][0]["current_value"] == before          # 저장되지 않았다
    rig.model.queue.append(ANSWER_OUT)
    out = rig.say("안 피움", turn_id="t-frozen")                                 # 풀린 뒤 같은 turn_id 재전송은 처리된다
    assert out.frontend_input["turn_failure_signal"] is None


def test_pausing_own_container_leaves_other_containers_running(request):
    """멈춤 대상은 이름으로만 고른다: 같은 옛 라벨(io.openit.dev-postgres=1)을 단 다른 컨테이너가 있어도 영향이 없다."""
    from . import pg_container as pc
    if not pc.docker_ready():
        pytest.skip("docker 또는 postgres:16 이미지 없음")
    other = pc.run_container("--label", "io.openit.dev-postgres=1", "--entrypoint", "sleep", pc.IMAGE, "600")
    request.addfinalizer(lambda: pc.stop(other))
    mine = pc.run_container("--entrypoint", "sleep", pc.IMAGE, "600")
    request.addfinalizer(lambda: pc.stop(mine))
    pc.pause(mine)
    try:
        assert pc.state(mine) == "paused"
        assert pc.state(other) == "running"
    finally:
        pc.unpause(mine)
    assert pc.state(mine) == "running"


def test_graceful_shutdown_finishes_in_flight_turn_and_readyz_drains(tmp_path, monkeypatch):
    """N8: SIGTERM 처리 순서(main과 같다: draining 표시 -> shutdown -> server_close -> app.stop). 처리 중 턴은 끝까지 처리되고, 그동안 /readyz는 503 shutting_down이다."""
    import http.client
    from survey_service.service import serve
    monkeypatch.setenv("SURVEY_API_TOKEN", "tok")
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "stok")
    app = _app(tmp_path, monkeypatch)
    httpd = serve(app, 0)
    assert httpd.request_queue_size >= 64          # 기본 5면 동시 접속이 몰릴 때 연결이 거부된다(부하 측정에서 확인)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    sess = app.registry.active().session
    real, gate, entered = sess.extract, threading.Event(), threading.Event()

    def slow(req):
        entered.set()
        gate.wait(10)
        return real(req)
    sess.extract = slow

    def post(path, body):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        c.request("POST", path, json.dumps(body).encode(), {"Authorization": "Bearer tok"})
        r = c.getresponse()
        return r.status, json.loads(r.read())
    assert post("/v1/sessions", {"session_id": "G1", "kind": "SESSION_START"})[0] == 200
    res: list = []
    th = threading.Thread(target=lambda: res.append(post("/v1/sessions/G1/turns", {"session_id": "G1", "kind": "UTTERANCE", "utterance": "안 피워요", "turn_id": "u1"})))
    th.start()
    assert entered.wait(5)
    app.draining.set()                             # SIGTERM
    ok, body = app.readiness()
    assert ok is False and "shutting_down" in body["problems"]
    stopper = threading.Thread(target=lambda: (httpd.shutdown(), httpd.server_close(), app.stop()))
    stopper.start()
    time.sleep(0.5)
    assert stopper.is_alive() and not res          # 처리 중인 요청을 기다린다
    gate.set()
    stopper.join(10)
    th.join(10)
    assert not stopper.is_alive() and res and res[0][0] == 200 and res[0][1]["turn_failure_signal"] is None


def test_readyz_total_deadline_with_three_hanging_dbs_and_reasons_are_kept(tmp_path, monkeypatch):
    """FAULT_POLICY 5: 의존이 응답하지 않아도 /readyz 총 시간은 마감(2초) + 작은 여유 안이다(허용 2.5초). 이전 구현은 +1초 여유로 3.1초(E2E 5pn).
    확인 셋이 모두 멈추면 사유 셋이 모두 나오고, 하나만 멈추거나 실패하면 그 사유만 나온다."""
    app = _app(tmp_path, monkeypatch)
    try:
        hang = threading.Event()
        monkeypatch.setattr(app.store, "ping", lambda: hang.wait(30))
        monkeypatch.setattr(app.outbox, "ping", lambda: hang.wait(30))
        monkeypatch.setattr(app.alert_store, "ping", lambda: hang.wait(30))
        ok, body, took = _timed(app)
        assert ok is False and took < 2.5, took
        assert {"session_db", "outbox_db", "alert_db"} <= set(body["problems"])
        def boom():
            raise RuntimeError("x")
        monkeypatch.setattr(app.store, "ping", boom)                  # 하나는 즉시 실패, 하나는 멈춤, 하나는 정상
        monkeypatch.setattr(app.alert_store, "ping", lambda: None)
        ok, body, took = _timed(app)
        assert ok is False and took < 2.5, took
        assert [p for p in body["problems"] if p.endswith("_db")] == ["session_db", "outbox_db"]
        hang.set()
    finally:
        app.stop() if hasattr(app, "stop") else None


def test_readyz_deadline_boundary_check_finishing_just_before_counts_ok_just_after_counts_failed(tmp_path, monkeypatch):
    """마감 경계: 마감(SURVEY_READYZ_DB_TIMEOUT_SECONDS)보다 조금 일찍 끝난 확인은 정상, 조금 늦게 끝난 확인은 실패로 센다. 총 시간은 마감 + 작은 여유 안이다."""
    app = _app(tmp_path, monkeypatch, extra={"SURVEY_READYZ_DB_TIMEOUT_SECONDS": "1.0"})
    try:
        budget = app.settings.readyz_db_timeout_seconds
        margin = 0.3
        monkeypatch.setattr(app.store, "ping", lambda: time.sleep(budget - margin))      # 마감 직전에 끝남
        monkeypatch.setattr(app.outbox, "ping", lambda: time.sleep(budget + margin))     # 마감 직후에 끝남
        monkeypatch.setattr(app.alert_store, "ping", lambda: None)
        ok, body, took = _timed(app)
        assert ok is False
        assert [p for p in body["problems"] if p.endswith("_db")] == ["outbox_db"]
        assert took < budget + margin, took
    finally:
        app.stop() if hasattr(app, "stop") else None


def test_readyz_deadline_measured_worst_case(tmp_path, monkeypatch):
    """세 확인이 모두 멈출 때 /readyz 총 시간을 5회 잰다. 최악 값은 `READYZ_MEASURE`로 출력한다(ADR-0011에 기록). 기본 마감 + 0.5초 안이어야 한다."""
    app = _app(tmp_path, monkeypatch)
    try:
        hang = threading.Event()
        for st in (app.store, app.outbox, app.alert_store):
            monkeypatch.setattr(st, "ping", lambda: hang.wait(30))
        took = [_timed(app)[2] for _ in range(5)]
        print("READYZ_MEASURE", [round(t, 3) for t in took], "worst", round(max(took), 3))
        assert max(took) < app.settings.readyz_db_timeout_seconds + 0.5, took
        hang.set()
    finally:
        app.stop() if hasattr(app, "stop") else None
