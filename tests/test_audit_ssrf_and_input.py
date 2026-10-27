"""감사 B6(웹 푸시 SSRF) 등 입력·네트워크 방어 시험."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from survey_service import netguard
from survey_service.alerts import WebPushSender

from .test_staff_alerts import _client_keys, _vapid_key_pem


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/x", "https://localhost/x", "https://10.0.0.5/x", "https://192.168.1.1/x", "https://172.16.0.9/x",
    "https://169.254.169.254/latest/meta-data", "https://[::1]/x", "https://[fd00::1]/x", "https://[::ffff:10.0.0.1]/x", "https://100.64.0.1/x",
    "https://intranet/x", "https://svc.internal/x", "https://user:pw@push.example.com/x", "https://push.example.com:8443/x", "http://push.example.com/x"])
def test_endpoint_syntax_rejects_internal_targets(url):
    assert netguard.endpoint_syntax_error(url)


def test_endpoint_syntax_accepts_public_names_and_addresses():
    assert netguard.endpoint_syntax_error("https://fcm.googleapis.com/fcm/send/abc") is None
    assert netguard.endpoint_syntax_error("https://8.8.8.8/x") is None


@pytest.mark.parametrize("url", ["https://127.0.0.1/x", "https://169.254.169.254/x", "https://10.1.2.3/x", "https://[::1]/x", "https://no-such-host.invalid/x"])
def test_host_is_public_false_for_internal_or_unresolvable(url):
    assert netguard.host_is_public(url) is False


def test_sender_refuses_internal_endpoint_without_connecting():
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(1)
            self.send_response(201); self.end_headers()
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        sender = WebPushSender(_vapid_key_pem(), "mailto:ops@example.com")            # 기본: 공개 주소만
        out = sender.send({"endpoint": f"https://127.0.0.1:{srv.server_address[1]}/x", "keys": _client_keys()}, b"{}")
        assert out == "FAILED" and not hits
    finally:
        srv.shutdown(); srv.server_close()


def test_sender_does_not_follow_redirects():
    hits = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self): hits.append("target")
        def do_POST(self): hits.append("target")
        def log_message(self, *a): pass
    tgt = HTTPServer(("127.0.0.1", 0), Target)
    tport = tgt.server_address[1]

    class Redir(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(307); self.send_header("Location", f"http://127.0.0.1:{tport}/inner"); self.send_header("Content-Length", "0"); self.end_headers()
        def log_message(self, *a): pass
    red = HTTPServer(("127.0.0.1", 0), Redir)
    for s in (tgt, red):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        sender = WebPushSender(_vapid_key_pem(), "mailto:ops@example.com", timeout=3, allow_private_hosts=True)
        out = sender.send({"endpoint": f"http://127.0.0.1:{red.server_address[1]}/x", "keys": _client_keys()}, b"{}")
        assert out == "FAILED" and not hits
    finally:
        for s in (tgt, red):
            s.shutdown(); s.server_close()


def test_push_subscription_api_rejects_internal_endpoint(tmp_path, monkeypatch):
    from .test_staff_alerts import http_call, make_app
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        keys = {"p256dh": "a", "auth": "b"}
        for ep in ("https://169.254.169.254/x", "https://localhost/x", "https://10.0.0.1/x"):
            code, body = http_call(port, "POST", "/v1/staff/push-subscriptions", {"endpoint": ep, "keys": keys})
            assert code == 400 and body["code"] == "INVALID_RECORD", ep
        code, _ = http_call(port, "POST", "/v1/staff/push-subscriptions", {"endpoint": "https://push.example.com/ok", "keys": keys})
        assert code == 201
    finally:
        app.draining.set(); httpd.shutdown(); httpd.server_close(); app.stop()


# ---------------------------------------------------------------- HTTP 계층 방어 (감사 A9·B4·E1)
import http.client
import logging
import socket
import time

from .test_staff_alerts import http_call, make_app


def _raw(port, request: bytes, timeout=5):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(request)
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" in data and (b"Content-Length" not in data or data.split(b"\r\n\r\n", 1)[1]):
                break
    except OSError:
        pass
    s.close()
    return data


def _shutdown(app, httpd):
    app.draining.set(); httpd.shutdown(); httpd.server_close(); app.stop()


def test_bad_content_length_and_surrogate_utterance_are_400_not_dropped_connections(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        for cl in (b"abc", b"-5", b"99999999"):
            r = _raw(port, b"POST /v1/sessions HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-api-token\r\nContent-Length: " + cl + b"\r\n\r\n")
            assert r.startswith(b"HTTP/1.0 400") or r.startswith(b"HTTP/1.1 400"), (cl, r[:60])
        assert http_call(port, "POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START"}, token="test-api-token")[0] == 200
        body = b'{"session_id":"K1","kind":"UTTERANCE","utterance":"\\ud800","turn_id":"t1"}'
        r = _raw(port, b"POST /v1/sessions/K1/turns HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-api-token\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        assert b" 400 " in r.split(b"\r\n", 1)[0]
    finally:
        _shutdown(app, httpd)


def test_request_id_header_and_json_log_field(tmp_path, monkeypatch):
    from survey_service.service import JsonFormatter, _REQ
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/healthz", headers={"X-Request-Id": "req-abcdef123456"})
        assert c.getresponse().getheader("X-Request-Id") == "req-abcdef123456"
        c.request("GET", "/healthz", headers={"X-Request-Id": "bad id!"})
        rid = c.getresponse().getheader("X-Request-Id")
        assert rid and rid != "bad id!" and len(rid) == 16                             # 형식이 어긋나면 새로 만든다
        _REQ.rid = "req-xyz-123456"
        line = JsonFormatter().format(logging.LogRecord("survey_service", logging.INFO, "f", 1, "메시지", None, None))
        assert json.loads(line)["request_id"] == "req-xyz-123456"
        _REQ.rid = None
    finally:
        _shutdown(app, httpd)


def test_auth_failure_is_logged_without_secrets_and_counted(tmp_path, monkeypatch, caplog):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        with caplog.at_level(logging.WARNING, logger="survey_service"):
            assert http_call(port, "POST", "/v1/sessions", {"session_id": "K9", "kind": "SESSION_START"}, token="wrong-secret-token")[0] == 401
            assert http_call(port, "GET", "/v1/staff/alerts", token="test-api-token")[0] == 403
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "요청 거절 status=401" in msgs and "요청 거절 status=403" in msgs and "wrong-secret-token" not in msgs and "K9" not in msgs
        st = app.readiness()[1]["api"]
        assert st["auth_failures"] >= 2 and st["requests"] >= 2
    finally:
        _shutdown(app, httpd)


def test_connection_cap_rejects_with_503_and_socket_timeout_frees_slow_clients(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch, extra={"SURVEY_SOCKET_TIMEOUT_SECONDS": "0.6", "SURVEY_MAX_CONNECTIONS": "2"})
    try:
        assert httpd.max_connections == 2
        idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]     # 요청을 보내지 않고 매달린 연결(slowloris)
        time.sleep(0.2)
        r = _raw(port, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b" 503 " in r.split(b"\r\n", 1)[0] and b"Retry-After" in r                # 상한 초과
        time.sleep(1.2)                                                                    # 읽기 제한(0.6초)으로 매달린 연결이 정리된다
        r = _raw(port, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b" 200 " in r.split(b"\r\n", 1)[0]
        for s in idle:
            s.close()
    finally:
        _shutdown(app, httpd)


def test_sse_connection_cap(tmp_path, monkeypatch):
    import urllib.error
    from .test_staff_alerts import Sse
    app, httpd, port = make_app(tmp_path, monkeypatch, extra={"SURVEY_MAX_SSE_CONNECTIONS": "1"})
    try:
        first = Sse(port)
        time.sleep(0.3)
        with pytest.raises(urllib.error.HTTPError) as e:
            Sse(port)
        assert e.value.code == 503 and e.value.headers.get("Retry-After")
        first.close()
    finally:
        _shutdown(app, httpd)


# ---------------------------------------------------------------- 2차 점검 L1·L2·L7
@pytest.mark.parametrize("url", [
    "https://[64:ff9b::7f00:1]/x", "https://[64:ff9b::a9fe:a9fe]/x", "https://[64:ff9b::a00:1]/x", "https://[2002:a9fe:a9fe::1]/x", "https://[2002:7f00:1::1]/x",
    "https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/x", "https://[64:ff9b:1::1]/x", "https://[::ffff:169.254.169.254]/x", "https://[::a9fe:a9fe]/x"])
def test_ssrf_guard_unwraps_embedded_ipv4(url):
    assert netguard.endpoint_syntax_error(url)
    assert netguard.host_is_public(url) is False


def test_ssrf_guard_allows_embedded_public_ipv4():
    assert netguard.endpoint_syntax_error("https://[64:ff9b::808:808]/x") is None          # NAT64로 감싼 8.8.8.8: 공개


def test_deeply_nested_body_is_400_not_a_dropped_connection(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        for depth in (600, 5000, 100000):
            body = ("[" * depth + "]" * depth).encode()
            r = _raw(port, b"POST /v1/sessions HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-api-token\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            assert b" 400 " in r.split(b"\r\n", 1)[0], (depth, r[:80])
        from survey_service.api import _has_surrogate
        deep = cur = []
        for _ in range(20000):
            nxt = []
            cur.append(nxt)
            cur = nxt
        assert _has_surrogate(deep) is False and _has_surrogate({"a": ["\ud800"]}) is True
    finally:
        _shutdown(app, httpd)


def test_connection_cap_503_has_request_id_and_schema_version(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch, extra={"SURVEY_SOCKET_TIMEOUT_SECONDS": "5", "SURVEY_MAX_CONNECTIONS": "1"})
    try:
        idle = socket.create_connection(("127.0.0.1", port))
        time.sleep(0.2)
        r = _raw(port, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b" 503 " in r.split(b"\r\n", 1)[0] and b"X-Request-Id: " in r and b"pipeline_schema_version" in r
        idle.close()
    finally:
        _shutdown(app, httpd)
