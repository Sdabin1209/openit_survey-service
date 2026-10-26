"""직원 알림(D64): 영속 저장, 목록, SSE(이어 받기), 웹 푸시 구독, 해결 표시, 권한, 전달 판정."""
import base64
import http.server
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

import pytest

from survey_service.alerts import AlertHub, AlertService, AlertStaffConsole, AlertStore, DeliveryFailed, WebPushSender
from survey_service.api import PublicApi
from survey_service.errors import StoreError
from survey_service.service import App, serve
from survey_service.settings import ConfigError, Settings

from .e2e_support import ANSWER_OUT, RISK_OUT, Rig, ScriptedRisk
from .fakes import FakeReleaseReader, FakeTraceIngest, RecordingDispatcher
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED

SECRET_TEXT = "숨이 안 쉬어져요"
AUTH, STAFF = "Bearer tok", "Bearer stok"


class FakePush:
    def __init__(self, result="ACCEPTED"):
        self.result, self.sent = result, []

    def send(self, subscription, payload):
        self.sent.append((dict(subscription), payload))
        return self.result


class AlertRig:
    def __init__(self, tmp_path, push=None, db="alerts.db"):
        self.rig = Rig(risk=True)
        self.store = AlertStore(str(tmp_path / db))
        self.hub = AlertHub()
        self.service = AlertService(self.store, self.hub, push, push_timeout=2.0)
        self.rig.orch.staff_console = AlertStaffConsole(self.service)
        self.api = PublicApi(self.rig.orch, "tok", staff_token="stok", alerts=self.service)

    def call(self, method, path, body=None, auth=STAFF):
        raw = json.dumps(body).encode() if body is not None else b""
        return self.api.handle(method, path, auth, raw)

    def start(self, sid="K1"):
        return self.call("POST", "/v1/sessions", {"session_id": sid, "kind": "SESSION_START"}, auth=AUTH)

    def risk(self, sid="K1", tid="t-risk"):
        self.rig.model.queue.append(RISK_OUT)
        return self.call("POST", f"/v1/sessions/{sid}/turns",
                         {"session_id": sid, "kind": "UTTERANCE", "utterance": SECRET_TEXT, "turn_id": tid}, auth=AUTH)

    def intervene(self, decision, tid, sid="K1", auth=STAFF):
        return self.call("POST", f"/v1/sessions/{sid}/intervention", {"session_id": sid, "decision": decision, "turn_id": tid}, auth=auth)


@pytest.fixture
def ar(tmp_path):
    r = AlertRig(tmp_path)
    yield r
    r.store.close()


# ---------------------------------------------------------------- 저장과 전달 판정
def test_risk_stop_is_persisted_with_only_session_and_kind(ar):
    ar.start()
    q = ar.hub.subscribe()
    code, fi, _ = ar.risk()
    assert code == 200 and fi["turn_failure_signal"] is None and fi["response_text"]
    code, body, _ = ar.call("GET", "/v1/staff/alerts")
    assert code == 200 and len(body["items"]) == 1 and body["next_cursor"] is None
    a = body["items"][0]
    assert set(a) == {"alert_id", "session_id", "kind", "created_at", "status", "resolved_by_decision", "resolved_at"}
    assert (a["session_id"], a["kind"], a["status"], a["resolved_by_decision"], a["resolved_at"]) == ("K1", "RISK_STOP", "OPEN", None, None)
    assert a["created_at"].endswith("Z") and a["alert_id"].startswith("al-")
    ev = q.get_nowait()
    assert ev["type"] == "alert" and ev["alert"] == a
    blob = json.dumps([a, ev, ar.store.events_after(0)], ensure_ascii=False)
    assert SECRET_TEXT not in blob and "utterance" not in blob              # 환자 발화·개인 정보 없음


def test_no_delivery_path_is_delivery_failed_but_alert_is_stored(ar):
    ar.start()
    code, fi, _ = ar.risk()                                                  # SSE 연결 없음, 푸시 없음
    assert code == 200 and fi["turn_failure_signal"] == {"session_id": "K1", "write_committed": "COMMITTED", "staff_alert": "DELIVERY_FAILED"}
    assert fi["response_text"]                                               # 응답 단계는 그대로
    items = ar.call("GET", "/v1/staff/alerts")[1]["items"]
    assert len(items) == 1 and items[0]["status"] == "OPEN"                  # 조회된다
    assert ar.rig.doc("K1")["progress_record"]["session_status"] == "PAUSED_RISK"
    assert ar.service.status()["delivery_failed"] == 1


def test_sse_connection_makes_it_delivered(ar):
    ar.start()
    ar.hub.subscribe()
    assert ar.risk()[1]["turn_failure_signal"] is None
    assert ar.service.status()["delivered"] == 1 and ar.service.status()["sse_connections"] == 1


def test_push_accepted_makes_it_delivered_and_payload_is_minimal(tmp_path):
    push = FakePush("ACCEPTED")
    r = AlertRig(tmp_path, push)
    try:
        r.store.upsert_subscription({"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}})
        r.start()
        assert r.risk()[1]["turn_failure_signal"] is None and len(push.sent) == 1
        sub, payload = push.sent[0]
        body = json.loads(payload)
        assert set(body) == {"type", "alert_id", "session_id", "kind"} and body["session_id"] == "K1" and body["kind"] == "RISK_STOP"
        assert SECRET_TEXT.encode() not in payload
    finally:
        r.store.close()


@pytest.mark.parametrize("result", ["FAILED", "GONE"])
def test_push_not_accepted_and_no_sse_is_delivery_failed(tmp_path, result):
    push = FakePush(result)
    r = AlertRig(tmp_path, push)
    try:
        r.store.upsert_subscription({"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}})
        r.start()
        assert r.risk()[1]["turn_failure_signal"]["staff_alert"] == "DELIVERY_FAILED"
        assert (r.store.count_subscriptions() == 0) == (result == "GONE")            # 만료된 구독은 지운다
    finally:
        r.store.close()


def test_without_push_configured_only_sse_counts(tmp_path):
    r = AlertRig(tmp_path, push=None)
    try:
        r.store.upsert_subscription({"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}})
        r.start()
        assert r.risk()[1]["turn_failure_signal"]["staff_alert"] == "DELIVERY_FAILED"
        assert r.service.status()["push_enabled"] is False
    finally:
        r.store.close()


def test_alert_store_failure_is_delivery_failed(ar):
    ar.start()
    ar.hub.subscribe()
    ar.store._db.close()                                                     # 저장 실패 = 전달 실패(연결이 있어도)
    code, fi, _ = ar.risk()
    assert fi["turn_failure_signal"]["staff_alert"] == "DELIVERY_FAILED" and fi["turn_failure_signal"]["write_committed"] == "COMMITTED"


def test_console_adapter_ignores_calls_without_alert(ar):
    c = AlertStaffConsole(ar.service)
    c.deliver({"staff_alert": None, "turn_failure_signal": None, "intervention_result": {"session_id": "K1", "result": "APPLIED"}})
    assert ar.store.list(None, 10, None)[0] == []
    with pytest.raises(DeliveryFailed):
        c.deliver({"staff_alert": {"session_id": "K1", "staff_alert": {"kind": "CALL_STAFF", "reasons": []}}})
    assert ar.store.list(None, 10, None)[0][0]["kind"] == "CALL_STAFF"


# ---------------------------------------------------------------- 영속
def test_alerts_and_events_survive_restart(tmp_path):
    r = AlertRig(tmp_path)
    r.start(); r.hub.subscribe(); r.risk()
    r.intervene("RESUME", "i-1")
    ids = [e["id"] for e in r.store.events_after(0)]
    r.store.close()
    s2 = AlertStore(str(tmp_path / "alerts.db"))
    items, _ = s2.list(None, 10, None)
    assert len(items) == 1 and items[0]["status"] == "RESOLVED" and items[0]["resolved_by_decision"] == "RESUME"
    assert [e["id"] for e in s2.events_after(0)] == ids == [1, 2]
    assert s2.latest_event_id() == 2
    s2.close()


def test_alert_and_event_rows_are_one_transaction(tmp_path):
    s = AlertStore(str(tmp_path / "a.db"))
    real = s._event
    def broken(etype, n):
        raise sqlite_error()
    import sqlite3
    sqlite_error = lambda: sqlite3.OperationalError("event write failed")
    s._event = broken
    with pytest.raises(StoreError):
        s.create("K1", "RISK_STOP")
    s._event = real
    assert s.list(None, 10, None)[0] == [] and s.events_after(0) == []         # 알림 행도 남지 않았다
    s.close()


def test_trim_events_keeps_alert_rows(tmp_path):
    s = AlertStore(str(tmp_path / "a.db"))
    for i in range(5):
        s.create(f"K{i}", "RISK_STOP")
    assert s.trim_events(keep=2) == 3 and [e["id"] for e in s.events_after(0)] == [4, 5]
    assert len(s.list(None, 10, None)[0]) == 5
    s.close()


# ---------------------------------------------------------------- 해결
def test_intervention_resolves_alert_and_publishes(ar):
    ar.start(); q = ar.hub.subscribe(); ar.risk(); q.get_nowait()
    code, fi, _ = ar.intervene("RESUME", "i-1")
    assert code == 200
    a = ar.call("GET", "/v1/staff/alerts?status=RESOLVED")[1]["items"][0]
    assert a["status"] == "RESOLVED" and a["resolved_by_decision"] == "RESUME" and a["resolved_at"].endswith("Z")
    ev = q.get_nowait()
    assert ev["type"] == "resolved" and ev["alert"] == a
    assert ar.call("GET", "/v1/staff/alerts?status=OPEN")[1]["items"] == []
    # 같은 turn_id 재전송은 같은 응답이고 다시 해결 이벤트를 만들지 않는다
    assert ar.intervene("RESUME", "i-1")[:2] == (code, fi) and q.empty()


def test_end_decision_resolves_with_end_and_failed_intervention_does_not(ar):
    ar.start(); ar.hub.subscribe(); ar.risk()
    assert ar.intervene("END", "i-x", sid="NOPE")[0] == 404                   # 모르는 세션: 해결 안 함
    ar.rig.orch.store = type("S", (), {"__getattr__": lambda s, n: getattr(ar.rig.store, n),
                                       "commit": lambda s, *a, **k: {"session_id": "K1", "status": "DB_ERROR", "write_outcome": "NOT_APPLIED", "revision": None, "detail": "x"}})()
    code, fi, _ = ar.intervene("END", "i-fail")                               # 쓰기 확정 실패 -> FAILED, 알림은 열린 채
    assert code == 200 and fi["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    assert ar.call("GET", "/v1/staff/alerts?status=OPEN")[1]["items"][0]["status"] == "OPEN"
    ar.rig.orch.store = ar.rig.store
    assert ar.intervene("END", "i-ok")[0] == 200
    assert ar.call("GET", "/v1/staff/alerts?status=RESOLVED")[1]["items"][0]["resolved_by_decision"] == "END"


def test_intervention_on_unpaused_session_leaves_other_alerts(ar):
    ar.start("A"); ar.start("B"); ar.hub.subscribe()
    ar.risk("A", "r-a"); ar.risk("B", "r-b")
    assert ar.intervene("RESUME", "i-1", sid="A")[0] == 200
    open_ = ar.call("GET", "/v1/staff/alerts?status=OPEN")[1]["items"]
    assert [a["session_id"] for a in open_] == ["B"]
    assert ar.intervene("RESUME", "i-2", sid="A")[0] == 409


def test_session_end_by_kiosk_resolves_without_decision(ar):
    ar.start(); ar.hub.subscribe(); ar.risk()
    ar.call("POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END"}, auth=AUTH)
    a = ar.call("GET", "/v1/staff/alerts")[1]["items"][0]
    assert a["status"] == "RESOLVED" and a["resolved_by_decision"] is None


# ---------------------------------------------------------------- 목록, 권한
def test_list_pagination_newest_first_and_validation(ar):
    for i in range(5):
        ar.store.create(f"K{i}", "RISK_STOP")
    ids = []
    cursor = None
    while True:
        code, body, _ = ar.call("GET", "/v1/staff/alerts?limit=2" + (f"&cursor={cursor}" if cursor else ""))
        assert code == 200
        ids += [a["session_id"] for a in body["items"]]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert ids == ["K4", "K3", "K2", "K1", "K0"]
    for bad in ("?status=DONE", "?limit=0", "?limit=201", "?limit=x", "?cursor=abc"):
        assert ar.call("GET", "/v1/staff/alerts" + bad)[0] == 400


def test_staff_routes_need_staff_token(ar):
    for method, path, body in [("GET", "/v1/staff/alerts", None),
                               ("POST", "/v1/staff/push-subscriptions", {"endpoint": "https://p/1", "keys": {"p256dh": "a", "auth": "b"}}),
                               ("DELETE", "/v1/staff/push-subscriptions?endpoint=https://p/1", None)]:
        assert ar.call(method, path, body, auth=AUTH)[0] == 403                 # 키오스크 토큰
        assert ar.call(method, path, body, auth="Bearer nope")[0] == 401
        assert ar.call(method, path, body, auth="")[0] == 401
    assert ar.api.stream_auth_error(AUTH)[0] == 403 and ar.api.stream_auth_error("")[0] == 401 and ar.api.stream_auth_error(STAFF) is None
    assert ar.store.count_subscriptions() == 0


def test_kiosk_routes_reject_staff_token_and_alert_routes_absent_without_service(ar):
    assert ar.call("POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START"}, auth=STAFF)[0] == 403
    bare = PublicApi(ar.rig.orch, "tok", staff_token="stok")
    assert bare.handle("GET", "/v1/staff/alerts", STAFF, b"")[0] == 404 and bare.stream_auth_error(STAFF)[0] == 404


# ---------------------------------------------------------------- 푸시 구독
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k1", "auth": "a1"}}


def test_subscription_register_is_idempotent_and_delete(ar):
    assert ar.call("POST", "/v1/staff/push-subscriptions", SUB)[:2] == (201, {"endpoint": SUB["endpoint"]})
    assert ar.call("POST", "/v1/staff/push-subscriptions", {**SUB, "keys": {"p256dh": "k2", "auth": "a2"}})[0] == 201     # 갱신
    subs = ar.store.subscriptions()
    assert len(subs) == 1 and subs[0]["keys"] == {"p256dh": "k2", "auth": "a2"}
    assert ar.call("DELETE", "/v1/staff/push-subscriptions?endpoint=" + SUB["endpoint"])[:2] == (200, {"endpoint": SUB["endpoint"], "deleted": True})
    assert ar.call("DELETE", "/v1/staff/push-subscriptions?endpoint=" + SUB["endpoint"])[1]["deleted"] is False       # 멱등
    assert ar.call("DELETE", "/v1/staff/push-subscriptions")[0] == 400


@pytest.mark.parametrize("body", [
    {"endpoint": "http://insecure/1", "keys": {"p256dh": "a", "auth": "b"}}, {"endpoint": "https://p/1"},
    {"endpoint": "https://p/1", "keys": {"p256dh": "a"}}, {"keys": {"p256dh": "a", "auth": "b"}},
    {"endpoint": "https://p/1", "keys": {"p256dh": "a", "auth": "b"}, "extra": 1}, {"endpoint": 5, "keys": {}},
])
def test_subscription_validation(ar, body):
    assert ar.call("POST", "/v1/staff/push-subscriptions", body)[0] == 400 and ar.store.count_subscriptions() == 0


def test_subscription_limit(ar):
    ar.api.max_subscriptions = 2
    for i in range(2):
        assert ar.call("POST", "/v1/staff/push-subscriptions", {**SUB, "endpoint": f"https://p/{i}"})[0] == 201
    assert ar.call("POST", "/v1/staff/push-subscriptions", {**SUB, "endpoint": "https://p/9"})[0] == 409
    assert ar.call("POST", "/v1/staff/push-subscriptions", {**SUB, "endpoint": "https://p/0"})[0] == 201           # 기존 구독 갱신은 허용


# ---------------------------------------------------------------- 실제 웹 푸시 전송(pywebpush) 로컬 푸시 서비스로
def _vapid_key_pem():
    from py_vapid import Vapid
    v = Vapid(); v.generate_keys()
    return v.private_pem().decode()


def _client_keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return {"p256dh": b64(pub), "auth": b64(os.urandom(16))}


class PushService:
    def __init__(self, status=201):
        self.status, self.requests = status, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((dict(self.headers), self.rfile.read(n)))
                self.send_response(outer.status); self.send_header("Content-Length", "0"); self.end_headers()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/push/1"

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


@pytest.mark.parametrize("status,expected", [(201, "ACCEPTED"), (410, "GONE"), (404, "GONE"), (500, "FAILED")])
def test_webpush_sender_against_local_push_service(status, expected):
    ps = PushService(status)
    try:
        sender = WebPushSender(_vapid_key_pem(), "mailto:ops@example.com", timeout=3)
        payload = json.dumps({"type": "alert", "alert_id": "al-1", "session_id": "K1", "kind": "RISK_STOP"}).encode()
        assert sender.send({"endpoint": ps.url, "keys": _client_keys()}, payload) == expected
        hdr, body = ps.requests[0]
        hdr = {k.lower(): v for k, v in hdr.items()}
        assert hdr["authorization"].lower().startswith("vapid t=") and hdr["content-encoding"] == "aes128gcm" and hdr["urgency"] == "high"
        assert SECRET_TEXT.encode() not in body and b"K1" not in body                      # 본문은 암호화되어 나간다
    finally:
        ps.close()


def test_webpush_sender_bad_subscription_is_failed_not_crash():
    sender = WebPushSender(_vapid_key_pem(), "mailto:ops@example.com")
    assert sender.send({"endpoint": "http://127.0.0.1:1/x", "keys": {"p256dh": "zz", "auth": "zz"}}, b"{}") == "FAILED"


def test_vapid_key_validation():
    with pytest.raises(ValueError):
        WebPushSender("not a key", "mailto:a@b.c")
    with pytest.raises(ValueError):
        WebPushSender(_vapid_key_pem(), "ops@example.com")


# ---------------------------------------------------------------- 서비스 조립, HTTP, SSE
def make_app(tmp_path, monkeypatch, vapid=None, subject="mailto:ops@example.com", hb="0.2"):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", sample_bank(), turn_engine_version=INSTALLED["turn_engine_version"],
                       model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    env = {"SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"), "SURVEY_WORK_DIR": str(tmp_path / "w"),
           "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake", "SURVEY_SESSION_IDLE_SECONDS": "0",
           "SURVEY_SSE_HEARTBEAT_SECONDS": hb, "SURVEY_VAPID_SUBJECT": subject}
    if vapid:
        monkeypatch.setenv("SURVEY_VAPID_PRIVATE_KEY", vapid)
    else:
        monkeypatch.delenv("SURVEY_VAPID_PRIVATE_KEY", raising=False)
    app = App(Settings.from_env(env), reader=reader, ingest=FakeTraceIngest(), dispatcher=RecordingDispatcher(), risk_source=ScriptedRisk())
    app.start()
    httpd = serve(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return app, httpd, httpd.server_address[1]


def http_call(port, method, path, body=None, token="test-staff-token", headers=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h.update(headers or {})
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


class Sse:
    """SSE 클라이언트: 이벤트를 큐에 쌓는다."""
    def __init__(self, port, last_id=None, token="test-staff-token"):
        h = {"Authorization": f"Bearer {token}"}
        if last_id is not None:
            h["Last-Event-ID"] = str(last_id)
        self.resp = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/staff/alerts/stream", headers=h), timeout=10)
        self.events, self.comments = queue.Queue(), []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        ev = {}
        try:
            for raw in self.resp:
                line = raw.decode().rstrip("\n")
                if line == "":
                    if "data" in ev:
                        self.events.put(ev)
                    ev = {}
                elif line.startswith(":"):
                    self.comments.append(line)
                elif ":" in line:
                    k, _, v = line.partition(":")
                    ev[k] = v.strip()
        except Exception:
            pass

    def next(self, timeout=5):
        ev = self.events.get(timeout=timeout)
        return int(ev["id"]), ev["event"], json.loads(ev["data"])

    def close(self):
        self.resp.close()


def api(port, method, path, body, token="test-api-token"):
    return http_call(port, method, path, body, token=token)


def start_and_risk(port, sid, tid):
    assert api(port, "POST", "/v1/sessions", {"session_id": sid, "kind": "SESSION_START"})[0] == 200
    return api(port, "POST", f"/v1/sessions/{sid}/turns", {"session_id": sid, "kind": "UTTERANCE", "utterance": SECRET_TEXT, "turn_id": tid})


@pytest.fixture
def live(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    yield app, port, tmp_path
    httpd.shutdown(); httpd.server_close(); app.stop()


def test_sse_streams_new_alerts_resolves_and_heartbeats(live):
    app, port, _ = live
    sse = Sse(port)
    time.sleep(0.2)
    code, fi = start_and_risk(port, "K1", "t-1")
    assert code == 200 and fi["turn_failure_signal"] is None                      # SSE 연결이 있어 DELIVERED
    i1, ev, alert = sse.next()
    assert ev == "alert" and alert["session_id"] == "K1" and alert["kind"] == "RISK_STOP" and i1 >= 1
    assert api(port, "POST", "/v1/sessions/K1/intervention", {"session_id": "K1", "decision": "RESUME", "turn_id": "i-1"}, token="test-staff-token")[0] == 200
    i2, ev2, alert2 = sse.next()
    assert ev2 == "resolved" and alert2["alert_id"] == alert["alert_id"] and alert2["resolved_by_decision"] == "RESUME" and i2 == i1 + 1
    time.sleep(0.6)
    assert any("ping" in c for c in sse.comments)
    sse.close()


def test_sse_resumes_with_last_event_id_and_after_restart(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        sse = Sse(port); time.sleep(0.2)
        start_and_risk(port, "A", "t-a")
        first_id, _, _ = sse.next()
        sse.close(); time.sleep(0.8)      # 서버는 다음 하트비트 쓰기에서 끊김을 안다
        code, fi = start_and_risk(port, "B", "t-b")                                # 끊긴 동안의 알림: 전달 경로 없음
        assert fi["turn_failure_signal"]["staff_alert"] == "DELIVERY_FAILED"
        start_and_risk(port, "C", "t-c")
        sse2 = Sse(port, last_id=first_id)
        got = [sse2.next() for _ in range(2)]
        assert [g[2]["session_id"] for g in got] == ["B", "C"] and [g[0] for g in got] == [first_id + 1, first_id + 2]
        sse2.close()
        last = got[-1][0]
    finally:
        httpd.shutdown(); httpd.server_close(); app.stop()
    # 서비스를 다시 띄워도 목록과 이어 받기가 된다
    app2, httpd2, port2 = make_app(tmp_path, monkeypatch)
    try:
        code, body = http_call(port2, "GET", "/v1/staff/alerts?status=OPEN")
        assert code == 200 and [a["session_id"] for a in body["items"]] == ["C", "B", "A"]
        sse3 = Sse(port2, last_id=first_id)
        assert [sse3.next()[2]["session_id"] for _ in range(2)] == ["B", "C"]
        start_and_risk(port2, "D", "t-d")
        i, ev, alert = sse3.next()
        assert alert["session_id"] == "D" and i == last + 1
        sse3.close()
    finally:
        httpd2.shutdown(); httpd2.server_close(); app2.stop()


def test_sse_and_list_over_http_need_staff_token(live):
    app, port, _ = live
    for token, code in [("test-api-token", 403), ("nope", 401), (None, 401)]:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/staff/alerts/stream",
                                                          headers={"Authorization": f"Bearer {token}"} if token else {}), timeout=5)
        assert e.value.code == code
        assert http_call(port, "GET", "/v1/staff/alerts", token=token)[0] == code


def test_subscription_endpoints_over_http(live):
    app, port, _ = live
    sub = {"endpoint": "https://push.example/z", "keys": {"p256dh": "a", "auth": "b"}}
    assert http_call(port, "POST", "/v1/staff/push-subscriptions", sub)[0] == 201
    assert http_call(port, "POST", "/v1/staff/push-subscriptions", sub)[0] == 201
    assert app.alert_store.count_subscriptions() == 1
    assert http_call(port, "DELETE", "/v1/staff/push-subscriptions?endpoint=https://push.example/z")[1]["deleted"] is True
    assert app.alert_store.count_subscriptions() == 0


def test_readyz_reports_alert_status_and_alert_db(live):
    app, port, _ = live
    code, body = http_call(port, "GET", "/readyz", token=None)
    st = body["staff_alerts"]
    assert code == 200 and st["push_enabled"] is False and st["sse_connections"] == 0 and "alert_db" not in body["problems"]


def test_vapid_from_environment(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch, vapid=_vapid_key_pem())
    try:
        assert app.alerts.status()["push_enabled"] is True
    finally:
        httpd.shutdown(); httpd.server_close(); app.stop()
    with pytest.raises(ConfigError):
        make_app(tmp_path / "b", monkeypatch, vapid="garbage")
    with pytest.raises(ConfigError):
        make_app(tmp_path / "c", monkeypatch, vapid=_vapid_key_pem(), subject="")


def test_replaceable_console_still_works(tmp_path, monkeypatch):
    """SURVEY_STAFF_CONSOLE 접점은 그대로 바꿔 꽂을 수 있다(기본은 알림 어댑터)."""
    from .fakes import RecordingConsole
    rec = RecordingConsole()
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", sample_bank(), turn_engine_version=INSTALLED["turn_engine_version"], model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    s = Settings(session_db=str(tmp_path / "s.db"), outbox_db=str(tmp_path / "o.db"), work_dir=str(tmp_path / "w"), release_reader="x:y",
                 trace_ingest="x:y", model_backend="fake", session_idle_seconds=0)
    app = App(s, reader=reader, ingest=FakeTraceIngest(), staff_console=rec)
    try:
        assert app.orchestrator.staff_console is rec
        app2 = None
    finally:
        app.stop()
