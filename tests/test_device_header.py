"""(C2: 신원 헤더는 게이트웨이 토큰 요청에서만 쓴다 - 아래 시험은 게이트웨이의 키오스크 토큰과 함께 X-Openit-Device를 보낸다. 토큰 없는 요청은 401.)
키오스크 기기 인증 = 게이트웨이 mTLS가 확인한 기기 ID를 `X-Openit-Device`로 받는다 (ADR 0012, D113, ADR-0031).
기대값은 계약 PUBLIC_API.md F절이다: 형식 ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$, 세션을 만든 기기만 접근(다른 기기는 404), 헤더가 토큰보다 우선, 둘 다 없으면 401.
호출은 계약 클라이언트 HttpPublicApi(device_id=...)로 한다."""
import json
import pytest
from boundary_contracts import BoundaryError
from boundary_contracts.clients import HttpPublicApi

from .http_server_support import stop_server
from .test_staff_alerts import http_call, make_app

KIOSK_TOKEN = "test-api-token-0123"          # conftest가 SURVEY_API_TOKEN으로 주는 2.x 호환 공유 토큰


@pytest.fixture
def svc(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    yield f"http://127.0.0.1:{port}", port
    app.draining.set(); stop_server(httpd); app.stop()


@pytest.fixture
def svc_mtls_only(tmp_path, monkeypatch):
    """게이트웨이 구성: 게이트웨이가 키오스크 토큰(upstream_token)을 붙이고 mTLS로 확인한 기기를 X-Openit-Device로 넘긴다(기기 토큰 표는 없다)."""
    app, httpd, port = make_app(tmp_path, monkeypatch)
    yield f"http://127.0.0.1:{port}", port
    app.draining.set(); stop_server(httpd); app.stop()


def code_of(fn, *a):
    with pytest.raises(BoundaryError) as e:
        fn(*a)
    return e.value.code


def test_device_header_starts_session_sends_utterance_and_reads_state_without_a_token(svc_mtls_only):
    url, _ = svc_mtls_only
    api = HttpPublicApi(url, token=KIOSK_TOKEN, device_id="kiosk-01")
    sid = "D1"
    assert api.start_session(sid)["turn_failure_signal"] is None
    assert api.send_utterance(sid, "안 먹어요", "t-1")["turn_failure_signal"] is None
    assert api.get_state(sid)["state"] in ("ACTIVE", "PAUSED_RISK", "ENDED")
    assert api.end_session(sid)["turn_failure_signal"] is None


def test_other_device_header_gets_404_on_every_session_route(svc_mtls_only):
    url, _ = svc_mtls_only
    a, b = HttpPublicApi(url, token=KIOSK_TOKEN, device_id="kiosk-01"), HttpPublicApi(url, token=KIOSK_TOKEN, device_id="kiosk-02")
    a.start_session("D2")
    assert code_of(b.get_state, "D2") == "NOT_FOUND"
    assert code_of(b.send_utterance, "D2", "안녕", "t-9") == "NOT_FOUND"
    assert code_of(b.end_session, "D2") == "NOT_FOUND"
    assert a.get_state("D2")["state"] == "ACTIVE"                      # 소유 기기는 그대로


def test_other_device_cannot_start_a_session_with_an_existing_id(svc_mtls_only):
    url, _ = svc_mtls_only
    HttpPublicApi(url, token=KIOSK_TOKEN, device_id="kiosk-01").start_session("D3")
    assert code_of(HttpPublicApi(url, token=KIOSK_TOKEN, device_id="kiosk-02").start_session, "D3") == "NOT_FOUND"


@pytest.mark.parametrize("bad", ["bad id!", "-x", ".x", "a" * 129, "caf\u00e9", ""])
def test_malformed_device_header_is_rejected(svc_mtls_only, bad):
    _, port = svc_mtls_only
    st, body = http_call(port, "POST", "/v1/sessions", {"session_id": "D4", "kind": "SESSION_START"}, token=KIOSK_TOKEN, headers={"X-Openit-Device": bad})
    assert st == 400 and body["code"] == "INVALID_RECORD"


def test_no_header_and_no_token_is_401(svc_mtls_only):
    url, _ = svc_mtls_only
    assert code_of(HttpPublicApi(url, device_id="kiosk-01").start_session, "D5") == "PERMISSION_DENIED"


def test_header_names_the_device_for_the_gateway_kiosk_token_and_the_token_alone_still_works(svc):
    url, _ = svc
    with_both = HttpPublicApi(url, token=KIOSK_TOKEN, device_id="kiosk-01")
    with_both.start_session("D6")
    assert code_of(HttpPublicApi(url, token=KIOSK_TOKEN, device_id="kiosk-02").get_state, "D6") == "NOT_FOUND"      # 토큰이 맞아도 헤더의 기기가 기준
    assert HttpPublicApi(url, token=KIOSK_TOKEN).start_session("D7")["turn_failure_signal"] is None                # 2.x 호환: 토큰만


def test_device_header_does_not_grant_staff_routes(svc_mtls_only):
    _, port = svc_mtls_only
    st, _ = http_call(port, "GET", "/v1/staff/alerts", token=None, headers={"X-Openit-Device": "kiosk-01"})
    assert st == 401


def test_identity_headers_are_ignored_on_every_non_gateway_token(tmp_path, monkeypatch):
    """CONVENTIONS 6절: X-Openit-User·Roles·Device는 게이트웨이 토큰(staff·result-reader·키오스크) 요청에서만 쓴다. 위험 신호 토큰·기기 토큰·모르는 토큰은 무시한다."""
    app, httpd, port = make_app(tmp_path, monkeypatch, extra={"SURVEY_RISK_SOURCE_TOKEN": "risk-token-0123456789", "SURVEY_RESULT_READER_TOKEN": "result-token-0123456789"})
    try:
        hdr = {"X-Openit-User": "intruder", "X-Openit-Roles": "staff,clinician", "X-Openit-Device": "kiosk-99"}
        for tok in ("risk-token-0123456789", "wrong-token-0123456789", None):
            st, _ = http_call(port, "GET", "/v1/staff/alerts", token=tok, headers=hdr)
            assert st in (401, 403), tok                          # 헤더가 무시되어 토큰 판정만 남는다
        st, body = http_call(port, "POST", "/v1/sessions/ZZ/risk-signals", {"signal_id": "s1", "source": "x", "code": "C"}, token="risk-token-0123456789", headers=hdr)
        assert st in (404, 403)                                   # 세션이 없다: 기기·사용자 헤더로 다른 결과를 얻지 못한다
        assert "intruder" not in json.dumps(app.alert_store.access_log("ZZ"))
    finally:
        app.draining.set(); stop_server(httpd); app.stop()


def test_dispatch_strips_identity_headers_before_any_non_gateway_token_reaches_a_handler():
    """변이에 민감한 단언: 라이브러리(핸들러)에 닿는 environ에 신원 헤더가 남는지 직접 본다."""
    from survey_service.components.link_public_api import PublicApi
    from survey_service.components.release_registry import ReleaseRegistry
    from survey_service.components.session_store import MemorySessionStore
    from survey_service.wiring.orchestrator import Orchestrator
    from .fakes import RecordingConsole
    from .timing import fake_clock
    api = PublicApi(Orchestrator(MemorySessionStore(clock=fake_clock()), ReleaseRegistry(), staff_console=RecordingConsole()), "kiosk-tok-0123456789",
                    staff_token="staff-tok-0123456789", result_token="result-tok-0123456789", risk_token="risk-tok-0123456789", device_tokens={"dev-1": "dev-tok-0123456789"})
    seen = {}

    class Link:
        def handle(self, environ):
            seen["env"] = dict(environ)
            return 200, {}, b""
    api._links["default"] = Link()
    base = {"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/staff/alerts", "HTTP_X_OPENIT_USER": "u1", "HTTP_X_OPENIT_ROLES": "staff", "HTTP_X_OPENIT_DEVICE": "k1"}
    heads = ("HTTP_X_OPENIT_USER", "HTTP_X_OPENIT_ROLES", "HTTP_X_OPENIT_DEVICE")
    for tok, gateway in (("kiosk-tok-0123456789", True), ("staff-tok-0123456789", True), ("result-tok-0123456789", True),
                         ("risk-tok-0123456789", False), ("dev-tok-0123456789", False), ("unknown-tok-0123456789", False), (None, False)):
        env = dict(base, **({"HTTP_AUTHORIZATION": f"Bearer {tok}"} if tok else {}))
        api.dispatch(env, api._links["default"])
        assert all((h in seen["env"]) == gateway for h in heads), (tok, gateway)
