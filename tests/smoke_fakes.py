"""컨테이너 스모크 시험의 생성자 배선. 운영 설정에는 모듈 경로를 넣지 않는다.
이미지에는 들어 있지 않고 스모크 스크립트가 tests/를 읽기 전용으로 붙인다. 참조 서버(HTTP)를 프로세스 안에서 띄우고
실제 HTTP 클라이언트(boundary_contracts.clients)로 붙으므로 이미지 안에서 I4·I6 HTTP 경로가 돈다."""
import tempfile
from pathlib import Path

from boundary_contracts.clients import HttpReleaseReader, HttpTraceIngest

from .e2e_support import ScriptedRisk
from .fakes import RecordingConsole, RecordingDispatcher
from .http_support import TOK, publish
from conformance.servers import ReferenceServer

_server = None


def _srv():
    global _server
    if _server is None:
        _server = ReferenceServer(TOK).start()
        publish(_server, Path(tempfile.mkdtemp(prefix="smoke-gate-")), "rel-smoke")
    return _server


def reader():
    return HttpReleaseReader(_srv().url, "r-tok", 5)


def ingest():
    return HttpTraceIngest(_srv().url, "w-tok", 5)


def console():
    return RecordingConsole()


def dispatcher():
    return RecordingDispatcher()


def risk_source():
    """스모크 전용 위험 입력원: 실제 입력원은 아직 없다(CR-012). 직원 알림 경로가 이미지 안에서 도는지만 본다."""
    return ScriptedRisk(("숨이 안 쉬어져요", "가슴이 너무 아파"))


if __name__ == "__main__":
    import os
    from survey_service.server import main
    from survey_service.wiring.app import App
    server = _srv()
    # The test fixture publishes its actual loopback HTTP addresses before the
    # standard entry point validates settings. HTTP owner clients remain real.
    os.environ.update(SURVEY_RELEASE_READER_URL=server.url, SURVEY_TRACE_INGEST_URL=server.url,
                      SURVEY_RELEASE_READER_TOKEN_ENV="SMOKE_GATE_TOKEN", SURVEY_TRACE_INGEST_TOKEN_ENV="SMOKE_INGEST_TOKEN",
                      SMOKE_GATE_TOKEN="r-tok", SMOKE_INGEST_TOKEN="w-tok")
    try:
        raise SystemExit(main(app_factory=lambda settings: App(settings, dispatcher=dispatcher(), risk_source=risk_source())))
    finally:
        server.stop()
