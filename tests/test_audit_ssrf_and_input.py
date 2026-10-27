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
