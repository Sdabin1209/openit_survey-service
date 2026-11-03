"""RUNTIME R3.18(웹 푸시): 재시도(429·5xx·불통 최대 3번·60초, Retry-After 또는 1·2·4초), 재시도 없음(401·403·그 밖 4xx·TLS·이름 해석), 404·410 삭제, Topic 헤더,
출구 터널(CONNECT), 등록 검증(PushSubscriptionRequest + RFC 6890), 로그에 endpoint·키 없음. 시간은 가짜 시계로(실제 시간을 재지 않는다)."""
import base64
import hashlib
import http.server
import json
import logging
import socket
import threading

import pytest

from survey_service.components.alert_service import topic_of
from survey_service.components.link_push import WebPushSender

from .test_staff_alerts import _client_keys, _vapid_key_pem, http_call, make_app
from .http_server_support import stop_server
from .test_tls_u99 import tls_server  # noqa: F401  (사설 루트로 서명한 로컬 TLS 서버)


class PushServer:
    """응답을 차례로 내는 로컬 푸시 서비스. 받은 요청의 머리글을 남긴다."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({k.lower(): v for k, v in self.headers.items()})
                status, hdr = outer.script.pop(0) if outer.script else (201, {})
                self.send_response(status)
                for k, v in hdr.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/push/1"

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def sleep(self, s):
        self.sleeps.append(s); self.t += s

    def now(self):
        return self.t


def sender(clock, **kw):
    return WebPushSender(_vapid_key_pem(), "mailto:ops@example.com", timeout=5.0, allow_private_hosts=True, sleep=clock.sleep, monotonic=clock.now, **kw)


def send(ps, clock, topic=None, **kw):
    return sender(clock, **kw).send({"endpoint": ps.url, "keys": _client_keys()}, b'{"session_id":"K1","kind":"RISK_STOP"}', topic=topic)


@pytest.fixture
def ps():
    made = []

    def make(script):
        p = PushServer(script); made.append(p); return p
    yield make
    for p in made:
        p.close()


def test_429_with_retry_after_waits_that_long_then_succeeds(ps):
    srv, clk = ps([(429, {"Retry-After": "2"}), (201, {})]), Clock()
    assert send(srv, clk) == "ACCEPTED" and len(srv.requests) == 2 and clk.sleeps == [2.0]


def test_5xx_without_retry_after_backs_off_1_2_and_gives_up_after_three_attempts(ps):
    srv, clk = ps([(503, {}), (500, {}), (502, {}), (201, {})]), Clock()
    assert send(srv, clk) == "FAILED" and len(srv.requests) == 3 and clk.sleeps == [1.0, 2.0]


def test_retry_after_beyond_the_sixty_second_budget_stops_retrying(ps):
    srv, clk = ps([(429, {"Retry-After": "120"}), (201, {})]), Clock()
    assert send(srv, clk) == "FAILED" and len(srv.requests) == 1 and clk.sleeps == []


@pytest.mark.parametrize("status", [400, 401, 403, 413])
def test_vapid_rejections_and_other_4xx_are_not_retried(ps, status):
    srv, clk = ps([(status, {}), (201, {})]), Clock()
    assert send(srv, clk) == "FAILED" and len(srv.requests) == 1 and clk.sleeps == []


@pytest.mark.parametrize("status", [404, 410])
def test_404_and_410_mean_gone_without_retry(ps, status):
    srv, clk = ps([(status, {}), (201, {})]), Clock()
    assert send(srv, clk) == "GONE" and len(srv.requests) == 1


def test_unreachable_endpoint_is_retried_within_the_same_limits():
    clk = Clock()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()      # 닫힌 포트: 연결 거부
    out = sender(clk).send({"endpoint": f"http://127.0.0.1:{port}/x", "keys": _client_keys()}, b"{}")
    assert out == "FAILED" and clk.sleeps == [1.0, 2.0]


def test_topic_header_is_sha256_first_24_bytes_base64url_32_chars_and_repeats_on_retry(ps):
    srv, clk = ps([(503, {}), (201, {})]), Clock()
    topic = topic_of("al-7")
    assert topic == base64.urlsafe_b64encode(hashlib.sha256(b"al-7").digest()[:24]).decode() and len(topic) == 32
    assert send(srv, clk, topic=topic) == "ACCEPTED"
    assert [r.get("topic") for r in srv.requests] == [topic, topic]


def test_name_that_does_not_resolve_is_not_retried_and_nothing_is_sent():
    clk = Clock()
    s = WebPushSender(_vapid_key_pem(), "mailto:ops@example.com", timeout=5.0, allow_private_hosts=False, sleep=clk.sleep, monotonic=clk.now)
    assert s.send({"endpoint": "https://no-such-host.invalid/x", "keys": _client_keys()}, b"{}") == "FAILED" and clk.sleeps == []


def test_tls_verification_failure_is_not_retried(tls_server):
    url, _root = tls_server
    clk = Clock()
    assert sender(clk).send({"endpoint": url + "/push", "keys": _client_keys()}, b"{}") == "FAILED" and clk.sleeps == []


def test_push_goes_only_through_the_egress_proxy_tunnel_and_a_refusal_is_not_retried():
    """CONNECT 터널: 자기 영역 게이트웨이가 허용하지 않으면 403으로 거절하고(R1.8) 다시 보내지 않는다. 프록시가 본 CONNECT 대상이 푸시 서비스의 호스트:443이다."""
    seen = []
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            data = c.recv(4096).decode("latin1")
            seen.append(data.split("\r\n", 1)[0])
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"); c.close()
    threading.Thread(target=serve, daemon=True).start()
    clk = Clock()
    s = WebPushSender(_vapid_key_pem(), "mailto:ops@example.com", timeout=5.0, allow_private_hosts=False, sleep=clk.sleep, monotonic=clk.now,
                      egress_proxy=f"http://127.0.0.1:{srv.getsockname()[1]}")
    try:
        assert s.send({"endpoint": "https://fcm.example.com/fcm/send/abc", "keys": _client_keys()}, b"{}") == "FAILED"
    finally:
        srv.close()
    assert seen == ["CONNECT fcm.example.com:443 HTTP/1.1"] and clk.sleeps == []


def test_egress_proxy_must_be_a_loopback_http_address():
    with pytest.raises(ValueError):
        WebPushSender(_vapid_key_pem(), "mailto:ops@example.com", egress_proxy="http://proxy.example.com:3128")


def test_logs_never_contain_the_endpoint_or_keys(ps, caplog):
    srv, clk = ps([(503, {}), (410, {})]), Clock()
    keys = _client_keys()
    with caplog.at_level(logging.DEBUG):
        sender(clk).send({"endpoint": srv.url, "keys": keys}, b"{}")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert srv.url not in text and keys["p256dh"] not in text and keys["auth"] not in text and "push/1" not in text


# ---------------------------------------------------------------- 등록 검증(PushSubscriptionRequest + RFC 6890)
@pytest.fixture
def live(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    yield app, port
    app.draining.set(); stop_server(httpd); app.stop()


@pytest.mark.parametrize("endpoint", ["https://169.254.169.254/x", "https://10.0.0.1/x", "https://[::1]/x", "https://localhost/x", "http://push.example.com/x",
                                      "https://push.example.com:8443/x", "https://private.push.example.com/x", "https://svc.internal/x", "https://no-such-host.invalid/x"])
def test_registration_rejects_special_addresses_names_resolving_to_them_wrong_scheme_and_port_with_400(live, endpoint):
    _, port = live
    st, body = http_call(port, "POST", "/v1/staff/push-subscriptions", {"endpoint": endpoint, "keys": _client_keys()})
    assert st == 400 and body["code"] == "INVALID_RECORD", endpoint


def test_registration_accepts_a_public_https_443_name(live):
    app, port = live
    st, body = http_call(port, "POST", "/v1/staff/push-subscriptions", {"endpoint": "https://fcm.example.com/fcm/send/abc", "keys": _client_keys()})
    assert st == 201 and app.alerts.store.count_subscriptions() == 1


# ---------------------------------------------------------------- 결과 기록과 staff_alert 합산(AlertService)
class RetryingPush:
    """첫 시도는 일시 장애(RETRY), 재시도에서 접수. topic·on_first를 받는다. gate를 열 때까지 재시도 결과를 미룬다(첫 대기가 끝난 뒤의 접수를 만든다)."""

    def __init__(self, gate=None):
        self.sent, self.topics, self.done, self.gate = [], [], threading.Event(), gate

    def send(self, subscription, payload, topic=None, on_first=None):
        self.topics.append(topic)
        if on_first:
            on_first("RETRY")
        if self.gate is not None:
            self.gate.wait(10)
        self.sent.append(payload)
        self.done.set()
        return "ACCEPTED"


def test_retry_that_finishes_inside_the_wait_counts_as_the_final_result(tmp_path):
    from .test_staff_alerts import AlertRig
    r = AlertRig(tmp_path, RetryingPush())
    try:
        r.store.upsert_subscription({"endpoint": "https://fcm.example.com/1", "keys": {"p256dh": "a", "auth": "b"}})
        r.start()
        assert r.risk()[1]["turn_failure_signal"]["staff_alert"] == "DELIVERED"      # 대기 안에 접수됐다: 합산은 DELIVERED
    finally:
        r.store.close()


def test_late_acceptance_after_the_wait_is_recorded_and_the_alert_carries_a_topic(tmp_path):
    from .test_staff_alerts import AlertRig
    from .timing import wait_until
    gate = threading.Event()
    push = RetryingPush(gate)
    r = AlertRig(tmp_path, push)
    r.service.push_timeout = 0.05                  # 첫 대기를 짧게 닫는다(재시도 접수는 그 뒤)
    try:
        r.store.upsert_subscription({"endpoint": "https://fcm.example.com/1", "keys": {"p256dh": "a", "auth": "b"}})
        r.start()
        fi = r.risk()[1]["turn_failure_signal"]
        assert fi["staff_alert"] == "DELIVERY_FAILED"            # 첫 대기 안에 접수가 없고 SSE 연결도 없다: 합산은 DELIVERY_FAILED
        gate.set()
        assert wait_until(lambda: r.service.stats["push_accepted"] == 1)          # 재시도에서 접수됨: 결과를 고친다
        a = r.store.alerts_of_session("K1")[0]
        assert a["last_delivery_status"] == "DELIVERED" and a["delivery_attempts"] >= 2
        assert push.topics[0] and len(push.topics[0]) == 32 and push.topics[0] == topic_of(a["alert_id"])
    finally:
        r.store.close()
