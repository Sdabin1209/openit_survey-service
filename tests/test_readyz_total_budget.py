"""/readyz는 의존이 응답하지 않아도(확인이 영영 끝나지 않아도, 주 연결의 보조 수치 읽기가 매달려도) 마감 뒤 사유와 함께 돌아온다. 계약 FAULT_POLICY 5.

실제 시간을 기다리지 않는다(U422): App이 시계를 주입받으므로(ADR-0050) 막힌 확인이 시작된 뒤 시험이 가짜 시계를 흘려 마감을 넘긴다. 판정은 돌아온 결과(사유, 오래된 수치 표시)다.
마감 값(2초)이 설정에서 오는지는 tests/test_readyz_db_outage.py::test_readyz_db_timeout_setting이, 확인 뒤 유예(STATS_GRACE_S) 값은 아래 마지막 시험이 본다.
"""
import threading

from .test_readyz_db_outage import _app, hanging, quick, ready_after_deadline
from .timing import FakeClock


def test_ok_checks_plus_hanging_stats_return_ok_with_stale_stats(tmp_path, monkeypatch):
    clock = FakeClock()
    app = _app(tmp_path, monkeypatch, clock=clock)
    gate, entered = threading.Event(), threading.Event()
    try:
        monkeypatch.setattr(app.exporter, "stats", hanging(gate, entered))         # 주 연결 읽기가 매달림
        started = [threading.Event() for _ in range(3)]
        for st, ev in zip((app.store, app.outbox, app.alert_store), started):
            monkeypatch.setattr(st, "ping", quick(st.ping, ev))               # 세 확인은 바로 통과한다: 그 결과가 나온 뒤에 마감을 넘긴다
        ok, body = ready_after_deadline(app, clock, [entered], list(zip(started, ("session_db", "outbox_db", "alert_db"))))
        assert ok and body["db_stats_stale"] is True
    finally:
        gate.set(); app.stop()


def test_hanging_checks_and_hanging_stats_return_the_reasons(tmp_path, monkeypatch):
    clock = FakeClock()
    app = _app(tmp_path, monkeypatch, clock=clock)
    gate = threading.Event()
    try:
        ents = [threading.Event() for _ in range(4)]
        for s, ev in zip((app.store, app.outbox, app.alert_store), ents):
            monkeypatch.setattr(s, "ping", hanging(gate, ev))
        monkeypatch.setattr(app.exporter, "stats", hanging(gate, ents[3]))
        ok, body = ready_after_deadline(app, clock, ents)
        assert not ok
        assert set(body["problems"]) >= {"session_db", "outbox_db", "alert_db"}
    finally:
        gate.set(); app.stop()


def test_healthy_readyz_still_reads_fresh_stats(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    try:
        ok, body = app.readiness()
        assert ok and body["db_stats_stale"] is False and body["trace_outbox"] is not None
    finally:
        app.stop()


def test_the_grace_for_hanging_stats_after_ok_checks_is_short():
    """분리 직후(확인은 통과, 주 연결은 매달림)의 200 응답이 마감을 다 쓰면 다음 요청의 503이 늦는다. 확인 뒤에는 짧은 유예만 기다린다: 유예 값을 직접 확인한다."""
    from survey_service.wiring.app import STATS_GRACE_S
    assert 0 < STATS_GRACE_S <= 0.5
