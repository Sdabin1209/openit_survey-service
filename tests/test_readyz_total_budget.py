"""/readyz 한 번의 응답 전체가 계약 FAULT_POLICY 5의 2초(`SURVEY_READYZ_DB_TIMEOUT_SECONDS`) 안에 끝난다.

기대값의 출처: 계약 FAULT_POLICY 5(의존이 응답하지 않으면 /readyz는 2초 안에 503, E2E 단언 허용 2.5초). 이전 구현은 DB 확인(총 2초)이 끝난 뒤
보조 수치 읽기(주 연결, 1초)를 **이어서** 기다려 최악 3초였다. 네트워크 분리 직후에는 확인용 새 연결은 아직 통과했는데 주 연결 읽기가 매달리는
요청이 생길 수 있고(이 경우 200), 그다음 요청이 확인에서 2초를 쓰므로 분리 시점부터 503까지 약 3초가 된다(gpu-dev E2E 5pn 2.8초 관찰과 맞는 경로).
"""
import time

from .test_readyz_db_outage import _app

BUDGET = 2.0
SLACK = 0.25      # 스레드 시작·직렬화 여유(측정 아님, 이 PC)


def _slow(seconds):
    def f(*a, **k):
        time.sleep(seconds)
    return f


def _timed(app):
    t = time.monotonic()
    ok, body = app.readiness()
    return ok, body, time.monotonic() - t


def test_slow_but_ok_checks_plus_hanging_stats_stay_within_the_total_budget(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    try:
        for s in (app.store, app.outbox, app.alert_store):
            monkeypatch.setattr(s, "ping", _slow(1.5))                  # 확인은 늦지만 통과
        monkeypatch.setattr(app.exporter, "stats", _slow(10.0))         # 주 연결 읽기가 매달림
        ok, body, el = _timed(app)
        assert el <= BUDGET + SLACK, el
        assert body["db_stats_stale"] is True
    finally:
        app.stop()


def test_hanging_checks_and_hanging_stats_stay_within_the_total_budget(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    try:
        for s in (app.store, app.outbox, app.alert_store):
            monkeypatch.setattr(s, "ping", _slow(10.0))
        monkeypatch.setattr(app.exporter, "stats", _slow(10.0))
        ok, body, el = _timed(app)
        assert not ok and el <= BUDGET + SLACK, el
        assert set(body["problems"]) >= {"session_db", "outbox_db", "alert_db"}
    finally:
        app.stop()


def test_healthy_readyz_still_reads_fresh_stats(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    try:
        ok, body, el = _timed(app)
        assert ok and body["db_stats_stale"] is False and body["trace_outbox"] is not None and el < 1.0
    finally:
        app.stop()


def test_ok_checks_do_not_wait_for_hanging_stats_beyond_a_short_grace(tmp_path, monkeypatch):
    """분리 직후(확인은 통과, 주 연결은 매달림)의 200 응답이 마감을 다 쓰면 다음 요청의 503이 늦는다. 확인 뒤에는 짧은 유예만 기다린다."""
    from survey_service.service import STATS_GRACE_S
    app = _app(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(app.exporter, "stats", _slow(10.0))
        ok, body, el = _timed(app)
        assert ok and body["db_stats_stale"] is True and el <= STATS_GRACE_S + SLACK, el
    finally:
        app.stop()
