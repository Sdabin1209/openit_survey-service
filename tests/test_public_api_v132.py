"""공개 API 보완(D66, boundary-contracts v1.3.2): 세션 상태, 푸시 설정, 오류 표, Retry-After, turn_id 봉투, 응답 스키마 검증."""
import json
import re
import threading
import time

import pytest
from boundary_contracts import validator

from survey_service.api import PublicApi
from survey_service.settings import Settings

from .e2e_support import Rig, item
from .test_staff_alerts import (AUTH, STAFF, AlertRig, Sse, _vapid_key_pem, http_call, make_app, start_and_risk, api)

V = {n: validator(n) for n in ("StaffAlert", "StaffAlertPage", "SessionState", "PushConfig", "BoundaryError")}


def valid(name, rec):
    errs = list(V[name].iter_errors(rec))
    assert not errs, (name, errs[0].message, rec)


@pytest.fixture
def ar(tmp_path):
    r = AlertRig(tmp_path)
    r.api.state_poll_min_interval = 0
    yield r
    r.store.close()


def state(r, sid="K1", auth=AUTH):
    return r.call("GET", f"/v1/sessions/{sid}/state", auth=auth)


# ---------------------------------------------------------------- 세션 상태
def test_state_active_paused_resume_text_and_ended(ar):
    assert state(ar)[0] == 404                                              # 모르는 세션
    ar.start()
    code, body, _ = state(ar)
    assert (code, body) == (200, {"session_id": "K1", "state": "ACTIVE", "resume_text": None}); valid("SessionState", body)
    ar.hub.subscribe(); ar.risk()
    code, body, _ = state(ar)
    assert (code, body) == (200, {"session_id": "K1", "state": "PAUSED_RISK", "resume_text": None}); valid("SessionState", body)
    code, fi, _ = ar.intervene("RESUME", "i-1")
    code, body, _ = state(ar)
    assert body["state"] == "ACTIVE" and body["resume_text"] == fi["response_text"] and body["resume_text"]; valid("SessionState", body)
    assert state(ar)[1] == body                                              # 다음 발화 턴이 접수되기 전까지 계속 준다
    ar.call("POST", "/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "안녕", "turn_id": "t-next"}, auth=AUTH)
    assert state(ar)[1] == {"session_id": "K1", "state": "ACTIVE", "resume_text": None}      # 접수된 뒤에는 null
    ar.call("POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END"}, auth=AUTH)
    body = state(ar)[1]
    assert body == {"session_id": "K1", "state": "ENDED", "resume_text": None}; valid("SessionState", body)


def test_state_after_staff_end_is_ended(ar):
    ar.start(); ar.hub.subscribe(); ar.risk()
    ar.intervene("END", "i-1")
    assert state(ar)[1]["state"] == "ENDED"


def test_resume_text_survives_restart_and_failed_resume_text(ar):
    ar.start(); ar.hub.subscribe(); ar.risk(); ar.intervene("RESUME", "i-1")
    before = state(ar)[1]
    api2 = PublicApi(ar.rig.orch, "tok", staff_token="stok", alerts=ar.service, state_poll_min_interval=0)   # 새 API 객체 = 메모리 상태 없음
    assert api2.handle("GET", "/v1/sessions/K1/state", AUTH, b"")[1] == before


def test_state_is_kiosk_only_and_validates_session_id(ar):
    ar.start()
    assert state(ar, auth=STAFF)[0] == 403 and state(ar, auth="")[0] == 401
    assert state(ar, sid="bad.id")[0] == 400 and state(ar, sid="a" * 65)[0] == 400


def test_state_polling_rate_is_limited_by_setting(ar):
    ar.start()
    ar.api.state_poll_min_interval = 5.0
    assert state(ar)[0] == 200
    code, body, hdr = state(ar)
    assert code == 429 and body["code"] == "UNAVAILABLE" and body["retryable"] is True and hdr["Retry-After"] == "1"
    assert state(ar, sid="K2")[0] == 404                                      # 다른 세션은 따로 센다
    ar.api.state_poll_min_interval = 0
    assert state(ar)[0] == 200


# ---------------------------------------------------------------- 푸시 설정
def test_push_config_null_without_vapid_and_public_key_with(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path / "n", monkeypatch)
    try:
        code, body = http_call(port, "GET", "/v1/staff/push-config")
        assert (code, body) == (200, {"vapid_public_key": None}); valid("PushConfig", body)
        assert http_call(port, "GET", "/v1/staff/push-config", token="test-api-token")[0] == 403
    finally:
        httpd.shutdown(); httpd.server_close(); app.stop()
    pem = _vapid_key_pem()
    app, httpd, port = make_app(tmp_path / "p", monkeypatch, vapid=pem)
    try:
        code, body = http_call(port, "GET", "/v1/staff/push-config")
        valid("PushConfig", body)
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid
        import base64
        raw = Vapid.from_pem(pem.encode()).public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        assert body["vapid_public_key"] == base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        assert "PRIVATE" not in json.dumps(body) and len(raw) == 65
    finally:
        httpd.shutdown(); httpd.server_close(); app.stop()


# ---------------------------------------------------------------- 오류 표, Retry-After
def test_status_code_to_boundary_error_code_table(ar):
    ar.start()
    cases = [
        (400, "INVALID_RECORD", ar.call("POST", "/v1/sessions", {"session_id": "K1", "kind": "NOPE"}, auth=AUTH)),
        (401, "PERMISSION_DENIED", ar.call("POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START"}, auth="")),
        (403, "PERMISSION_DENIED", ar.call("GET", "/v1/staff/alerts", auth=AUTH)),
        (404, "NOT_FOUND", ar.call("GET", "/v1/sessions/NOPE/state", auth=AUTH)),
        (409, "CONFLICT", ar.intervene("RESUME", "i-1")),                                   # 멈추지 않은 세션
    ]
    for status, code, (s, body, _) in cases:
        assert s == status and body["code"] == code, (status, body)
        valid("BoundaryError", body)
        assert body["retryable"] is False
    # 429·503은 UNAVAILABLE + 정수 Retry-After (설정으로 바꿀 수 있다)
    ar.api.retry_after = 7
    ar.api.state_poll_min_interval = 5.0
    state(ar)
    s, body, hdr = state(ar)
    assert (s, body["code"], body["retryable"], hdr["Retry-After"]) == (429, "UNAVAILABLE", True, "7")
    ar.store._db.close()
    s, body, hdr = ar.call("GET", "/v1/staff/alerts")
    assert (s, body["code"], hdr["Retry-After"]) == (503, "UNAVAILABLE", "7") and re.fullmatch(r"\d+", hdr["Retry-After"])
    valid("BoundaryError", body)


def test_inflight_overflow_is_429_unavailable_with_retry_after():
    rig = Rig()
    api_ = PublicApi(rig.orch, "tok", max_inflight=1, retry_after_seconds=3)
    api_.handle("POST", "/v1/sessions", AUTH, json.dumps({"session_id": "K1", "kind": "SESSION_START"}).encode())
    gate, entered = threading.Event(), threading.Event()
    def slow(inp):
        entered.set(); gate.wait(5); return {"items": []}
    rig.release.session.parse = slow
    body = json.dumps({"session_id": "K1", "kind": "UTTERANCE", "utterance": "x", "turn_id": "t1"}).encode()
    t = threading.Thread(target=lambda: api_.handle("POST", "/v1/sessions/K1/turns", AUTH, body)); t.start()
    assert entered.wait(5)
    s, b, h = api_.handle("POST", "/v1/sessions/K1/turns", AUTH, body.replace(b"t1", b"t2"))
    gate.set(); t.join()
    assert (s, b["code"], b["retryable"], h["Retry-After"]) == (429, "UNAVAILABLE", True, "3")


def test_503_without_release_has_retry_after():
    from survey_service.orchestrator import Orchestrator
    from survey_service.release import ReleaseRegistry
    from survey_service.store.memory import MemorySessionStore
    from .fakes import RecordingConsole
    api_ = PublicApi(Orchestrator(MemorySessionStore(), ReleaseRegistry(), staff_console=RecordingConsole()), "tok")
    s, b, h = api_.handle("POST", "/v1/sessions", AUTH, json.dumps({"session_id": "K1", "kind": "SESSION_START"}).encode())
    assert (s, b["code"], h["Retry-After"]) == (503, "UNAVAILABLE", "1")


# ---------------------------------------------------------------- turn_id는 봉투 필드
def test_turn_id_is_stripped_before_schema_validation(ar):
    ar.start()
    # 봉투를 뗀 나머지가 문진 스키마를 통과해야 한다: 모르는 필드는 거부, turn_id는 통과
    ok = {"session_id": "K1", "kind": "UTTERANCE", "utterance": "안녕", "turn_id": "t-1"}
    assert ar.call("POST", "/v1/sessions/K1/turns", ok, auth=AUTH)[0] == 200
    assert ar.call("POST", "/v1/sessions/K1/turns", {**ok, "turn_id": "t-2", "extra": 1}, auth=AUTH)[0] == 400
    assert ar.call("POST", "/v1/sessions/K1/turns", {k: v for k, v in ok.items() if k != "turn_id"}, auth=AUTH)[0] == 400
    # 개입도 같다: InterventionDecision 스키마(session_id, decision)로 검증
    ar.hub.subscribe(); ar.risk()
    bad = {"session_id": "K1", "decision": "RESUME", "turn_id": "i-1", "note": "x"}
    assert ar.call("POST", "/v1/sessions/K1/intervention", bad)[0] == 400
    assert ar.call("POST", "/v1/sessions/K1/intervention", {"session_id": "K1", "decision": "RESUME"})[0] == 400      # turn_id 없음
    assert ar.intervene("RESUME", "i-1")[0] == 200


def test_start_and_end_accept_an_optional_turn_id(ar):
    s1 = ar.call("POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START", "turn_id": "my-start"}, auth=AUTH)
    assert s1[0] == 200
    again = ar.call("POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START", "turn_id": "my-start"}, auth=AUTH)
    assert again[:2] == s1[:2] and ar.rig.store.get_turn("K1", "my-start") is not None and ar.rig.store.get_turn("K1", "start") is None
    assert ar.call("POST", "/v1/sessions", {"session_id": "K2", "kind": "SESSION_START", "turn_id": "bad id"}, auth=AUTH)[0] == 400
    assert ar.call("POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END", "turn_id": "my-end"}, auth=AUTH)[0] == 200
    assert ar.rig.store.get_turn("K1", "my-end") is not None


# ---------------------------------------------------------------- 응답 스키마 검증 (boundary-contracts)
def test_alert_list_and_pages_validate_against_boundary_schemas(ar):
    for i in range(4):
        ar.store.create(f"K{i}", "RISK_STOP")
    ar.store.resolve("K0", "END")
    cursor, seen = None, 0
    while True:
        code, body, _ = ar.call("GET", "/v1/staff/alerts?limit=3" + (f"&cursor={cursor}" if cursor else ""))
        assert code == 200; valid("StaffAlertPage", body)
        for a in body["items"]:
            valid("StaffAlert", a); seen += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert seen == 4
    default = ar.call("GET", "/v1/staff/alerts")[1]; valid("StaffAlertPage", default)
    resolved = ar.call("GET", "/v1/staff/alerts?status=RESOLVED")[1]
    valid("StaffAlertPage", resolved)
    assert resolved["items"][0]["resolved_by_decision"] == "END"


def test_sse_events_are_alert_and_resolved_with_event_numbers_and_validate(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        sse = Sse(port); time.sleep(0.2)
        start_and_risk(port, "K1", "t-1")
        i1, ev1, a1 = sse.next()
        assert api(port, "POST", "/v1/sessions/K1/intervention", {"session_id": "K1", "decision": "RESUME", "turn_id": "i-1"}, token="test-staff-token")[0] == 200
        i2, ev2, a2 = sse.next()
        start_and_risk(port, "K2", "t-2")
        i3, ev3, a3 = sse.next()
        assert (ev1, ev2, ev3) == ("alert", "resolved", "alert") and (i1, i2, i3) == (i1, i1 + 1, i1 + 2)     # 이벤트 순번
        for a in (a1, a2, a3):
            valid("StaffAlert", a)
        assert a2["status"] == "RESOLVED" and a2["alert_id"] == a1["alert_id"] != a3["alert_id"]
        sse.close()
        resumed = Sse(port, last_id=i1)                        # 이벤트 순번 기준 이어 받기
        assert [resumed.next()[1:2][0] for _ in range(2)] == ["resolved", "alert"]
        resumed.close()
    finally:
        httpd.shutdown(); httpd.server_close(); app.stop()


def test_new_settings_come_from_environment():
    base = {"SURVEY_RELEASE_READER": "a:b", "SURVEY_TRACE_INGEST": "c:d", "SURVEY_MODEL_BACKEND": "fake"}
    s = Settings.from_env({**base, "SURVEY_SESSION_IDLE_SECONDS": "120", "SURVEY_STATE_POLL_MIN_INTERVAL_SECONDS": "1.5",
                           "SURVEY_RETRY_AFTER_SECONDS": "4", "SURVEY_SSE_HEARTBEAT_SECONDS": "10"})
    assert (s.session_idle_seconds, s.state_poll_min_interval_seconds, s.retry_after_seconds, s.sse_heartbeat_seconds) == (120, 1.5, 4, 10.0)
    d = Settings.from_env(base)
    assert (d.session_idle_seconds, d.state_poll_min_interval_seconds, d.retry_after_seconds, d.sse_heartbeat_seconds) == (300, 0.5, 1, 15.0)
