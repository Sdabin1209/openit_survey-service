"""HTTP 서버가 구현(표준 라이브러리 ThreadingHTTPServer, waitress)과 무관하게 지켜야 하는 성질 (ADR-0004, ADR-0018, ADR-0029).
1) 요청을 다 보내지 않고 매달린 연결(slowloris)이 다른 요청을 막지 않는다. 2) 종료를 요청해도 처리 중인 요청은 응답을 받는다."""
import socket
import threading
import time

from .http_server_support import stop_server
from .test_staff_alerts import http_call, make_app


def test_idle_partial_connections_do_not_block_other_requests(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    socks = []
    try:
        for _ in range(20):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"POST /v1/sessions HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n")      # 헤더를 끝내지 않는다
            socks.append(s)
        t0 = time.monotonic()
        for _ in range(5):
            assert http_call(port, "GET", "/healthz", token=None)[0] == 200
        assert time.monotonic() - t0 < 3.0
    finally:
        for s in socks:
            s.close()
        app.draining.set(); stop_server(httpd); app.stop()


def test_in_flight_request_gets_its_response_when_shutdown_is_requested(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    started, result = threading.Event(), {}
    real = app.api.dispatch      # 서버는 api.dispatch(openit_link.App 통과)로 요청을 처리한다

    def slow(*a, **kw):
        started.set()
        time.sleep(1.0)
        return real(*a, **kw)
    monkeypatch.setattr(app.api, "dispatch", slow)

    def call():
        result["r"] = http_call(port, "GET", "/v1/staff/alerts")
    th = threading.Thread(target=call)
    th.start()
    try:
        assert started.wait(5)
        app.draining.set()
        stop_server(httpd)                       # 종료 요청: 처리 중이던 요청이 끝나기를 기다린다
        th.join(5)
        assert result.get("r") and result["r"][0] == 200, result
    finally:
        app.stop()
