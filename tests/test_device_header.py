"""키오스크 기기 인증 = 게이트웨이 mTLS가 확인한 기기 ID를 `X-Openit-Device`로 받는다 (ADR 0012, D113, ADR-0031).
기대값은 계약 PUBLIC_API.md F절이다: 형식 ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$, 세션을 만든 기기만 접근(다른 기기는 404), 헤더가 토큰보다 우선, 둘 다 없으면 401.
호출은 계약 클라이언트 HttpPublicApi(device_id=...)로 한다."""
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
    """게이트웨이 mTLS만 쓰는 구성: 키오스크 토큰도 기기 토큰 표도 없다."""
    monkeypatch.delenv("SURVEY_API_TOKEN", raising=False)
    app, httpd, port = make_app(tmp_path, monkeypatch)
    yield f"http://127.0.0.1:{port}", port
    app.draining.set(); stop_server(httpd); app.stop()


def code_of(fn, *a):
    with pytest.raises(BoundaryError) as e:
        fn(*a)
    return e.value.code


def test_device_header_starts_session_sends_utterance_and_reads_state_without_a_token(svc_mtls_only):
    url, _ = svc_mtls_only
    api = HttpPublicApi(url, device_id="kiosk-01")
    sid = "D1"
    assert api.start_session(sid)["turn_failure_signal"] is None
    assert api.send_utterance(sid, "안 먹어요", "t-1")["turn_failure_signal"] is None
    assert api.get_state(sid)["state"] in ("ACTIVE", "PAUSED_RISK", "ENDED")
    assert api.end_session(sid)["turn_failure_signal"] is None


def test_other_device_header_gets_404_on_every_session_route(svc_mtls_only):
    url, _ = svc_mtls_only
    a, b = HttpPublicApi(url, device_id="kiosk-01"), HttpPublicApi(url, device_id="kiosk-02")
    a.start_session("D2")
    assert code_of(b.get_state, "D2") == "NOT_FOUND"
    assert code_of(b.send_utterance, "D2", "안녕", "t-9") == "NOT_FOUND"
    assert code_of(b.end_session, "D2") == "NOT_FOUND"
    assert a.get_state("D2")["state"] == "ACTIVE"                      # 소유 기기는 그대로


def test_other_device_cannot_start_a_session_with_an_existing_id(svc_mtls_only):
    url, _ = svc_mtls_only
    HttpPublicApi(url, device_id="kiosk-01").start_session("D3")
    assert code_of(HttpPublicApi(url, device_id="kiosk-02").start_session, "D3") == "NOT_FOUND"


@pytest.mark.parametrize("bad", ["bad id!", "-x", ".x", "a" * 129, "caf\u00e9", ""])
def test_malformed_device_header_is_rejected(svc_mtls_only, bad):
    _, port = svc_mtls_only
    st, body = http_call(port, "POST", "/v1/sessions", {"session_id": "D4", "kind": "SESSION_START"}, token=None, headers={"X-Openit-Device": bad})
    assert st == 400 and body["code"] == "INVALID_RECORD"


def test_no_header_and_no_token_is_401(svc_mtls_only):
    url, _ = svc_mtls_only
    assert code_of(HttpPublicApi(url).start_session, "D5") == "PERMISSION_DENIED"


def test_header_wins_over_a_compat_token_and_tokens_still_work_alone(svc):
    url, _ = svc
    with_both = HttpPublicApi(url, token=KIOSK_TOKEN, device_id="kiosk-01")
    with_both.start_session("D6")
    assert code_of(HttpPublicApi(url, token=KIOSK_TOKEN, device_id="kiosk-02").get_state, "D6") == "NOT_FOUND"      # 토큰이 맞아도 헤더의 기기가 기준
    assert HttpPublicApi(url, token=KIOSK_TOKEN).start_session("D7")["turn_failure_signal"] is None                # 2.x 호환: 토큰만


def test_device_header_does_not_grant_staff_routes(svc_mtls_only):
    _, port = svc_mtls_only
    st, _ = http_call(port, "GET", "/v1/staff/alerts", token=None, headers={"X-Openit-Device": "kiosk-01"})
    assert st == 401
