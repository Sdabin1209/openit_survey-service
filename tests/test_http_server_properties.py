"""HTTP 서버 성질 (ADR-0004, ADR-0018, ADR-0029).
느린·반쯤 열린 연결을 서버가 몇 초에 끊는가, 접속 상한 때 새 연결을 어떻게 다루는가는 waitress의 동작이다(라이브러리): 시험하지 않는다(U421).
이 영역의 책임은 (1) waitress에 올바른 값을 넘기는 것 — 아래 설정 값 확인, (2) 종료를 요청해도 처리 중인 요청이 응답을 받도록 close_server가 기다리는 것 — 사건(Event)으로 본다."""
import threading

from .timing import HANG_GUARD_SECONDS
from .http_server_support import stop_server
from .test_staff_alerts import http_call, make_app


def test_waitress_gets_the_connection_and_timeout_values_from_settings(tmp_path, monkeypatch):
    """설정(SURVEY_SOCKET_TIMEOUT_SECONDS, SURVEY_MAX_CONNECTIONS, 요청 동시 처리 상한)이 waitress 인자로 그대로 넘어간다. 시간을 재지 않고 넘긴 값을 본다."""
    import math
    from survey_service.server import _WAITRESS_INTERNAL_FDS
    app, httpd, port = make_app(tmp_path, monkeypatch, extra={"SURVEY_SOCKET_TIMEOUT_SECONDS": "7", "SURVEY_MAX_CONNECTIONS": "11"})
    try:
        st = app.settings
        adj = httpd.adj
        assert adj.channel_timeout == max(1, math.ceil(st.socket_timeout_seconds)) == 7
        assert adj.connection_limit == st.max_connections + _WAITRESS_INTERNAL_FDS == 13
        assert adj.threads == st.api_max_inflight + st.max_sse_connections + 4
    finally:
        app.draining.set(); stop_server(httpd); app.stop()


def test_in_flight_request_gets_its_response_when_shutdown_is_requested(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    started, gate, result = threading.Event(), threading.Event(), {}
    real = app.api.dispatch      # 서버는 api.dispatch(openit_link.App 통과)로 요청을 처리한다

    def slow(*a, **kw):
        started.set()
        gate.wait(HANG_GUARD_SECONDS)           # 매달림 감시: 종료를 요청한 뒤에 풀어 준다(고정 sleep은 부하에서 순서가 바뀐다)
        return real(*a, **kw)
    monkeypatch.setattr(app.api, "dispatch", slow)

    def call():
        result["r"] = http_call(port, "GET", "/v1/staff/alerts")
    th = threading.Thread(target=call)
    th.start()
    try:
        assert started.wait(HANG_GUARD_SECONDS)                  # 매달림 감시
        app.draining.set()
        stopper = threading.Thread(target=lambda: stop_server(httpd))
        stopper.start()                          # 종료 요청: 처리 중이던 요청이 끝나기를 기다린다
        gate.set()
        stopper.join(HANG_GUARD_SECONDS)                        # 매달림 감시
        th.join(HANG_GUARD_SECONDS)
        assert result.get("r") and result["r"][0] == 200, result
    finally:
        gate.set()
        app.draining.set(); app.stop()
