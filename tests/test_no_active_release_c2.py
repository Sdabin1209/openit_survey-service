"""PUBLIC_API(3.4.4): 활성 릴리스가 없으면 GET /v1/question-sets 와 POST /v1/sessions 는 503 UNAVAILABLE + details.reason=NO_ACTIVE_RELEASE + Retry-After(빈 목록 200이 아니다)."""
import json

from survey_service.components.link_public_api import PublicApi
from survey_service.components.release_registry import ReleaseRegistry
from survey_service.components.session_store import MemorySessionStore
from survey_service.wiring.orchestrator import Orchestrator

from .fakes import RecordingConsole
from .timing import fake_clock


def _api():
    return PublicApi(Orchestrator(MemorySessionStore(clock=fake_clock()), ReleaseRegistry(), staff_console=RecordingConsole()), "tok")


def test_question_sets_and_session_start_without_an_active_release_are_503_no_active_release():
    api = _api()
    for method, path, body in (("GET", "/v1/question-sets", b""), ("POST", "/v1/sessions", json.dumps({"kind": "SESSION_START"}).encode())):
        code, out, hdr = api.handle(method, path, "Bearer tok", body, device="test-kiosk")
        assert code == 503 and out["code"] == "UNAVAILABLE" and out["details"]["reason"] == "NO_ACTIVE_RELEASE", (path, code, out)
        assert hdr.get("Retry-After") and int(hdr["Retry-After"]) >= 1
