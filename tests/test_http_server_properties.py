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


def test_run_server_swallows_the_ebadf_of_a_requested_close_and_raises_otherwise():
    """close_server가 다른 스레드에서 소켓을 닫으면 waitress 루프의 select가 EBADF(OSError)로 깨어난다. 닫기를 요청한 뒤면 정상 종료, 아니면 그대로 올린다(S5)."""
    import pytest
    from survey_service.server import run_server

    class S:
        def __init__(self, closed):
            self._survey_closed = closed

        def run(self):
            raise OSError(9, "Bad file descriptor")
    run_server(S(True))
    with pytest.raises(OSError):
        run_server(S(False))


def test_server_binds_only_to_the_configured_host(tmp_path, monkeypatch):
    """SURVEY_HOST=127.0.0.1이면 그 주소에만 붙는다(다른 인터페이스 주소로는 닿지 않는다). 기본은 모든 인터페이스."""
    import socket
    from survey_service.server import serve
    from survey_service.settings import Settings
    seen = {}
    import survey_service.server as srv
    monkeypatch.setattr(srv, "create_server", lambda *a, **k: seen.update(k) or object())
    monkeypatch.setattr(srv, "make_wsgi", lambda app: None)
    base = {"SURVEY_RELEASE_READER": "http", "SURVEY_RELEASE_READER_URL": "http://127.0.0.1:1", "SURVEY_TRACE_INGEST": "http", "SURVEY_TRACE_INGEST_URL": "http://127.0.0.1:1", "SURVEY_MODEL_BACKEND": "fake"}
    for env, want in (({**base, "SURVEY_HOST": "127.0.0.1"}, "127.0.0.1"), (base, "0.0.0.0")):
        serve(type("A", (), {"settings": Settings.from_env(env)})(), 0)
        assert seen["host"] == want
    # 실제 소켓: 127.0.0.1에 붙은 waitress는 이 PC의 다른 주소로는 연결을 받지 않는다
    from waitress import create_server
    h = create_server(lambda e, s: (s("200 OK", []), [b"x"])[1], host="127.0.0.1", port=0)
    other = [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) if not a[4][0].startswith("127.")]
    try:
        for ip in other[:1]:
            with pytest.raises(OSError):
                socket.create_connection((ip, h.effective_port), timeout=1).close()
    finally:
        h.close()
