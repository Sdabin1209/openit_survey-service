"""컨테이너 스모크 시험이 환경 변수로 꽂는 팩토리(SURVEY_RELEASE_READER=tests.smoke_fakes:reader 등).
이미지에는 들어 있지 않고 스모크 스크립트가 tests/를 읽기 전용으로 붙인다. 참조 서버(HTTP)를 프로세스 안에서 띄우고
실제 HTTP 클라이언트(boundary_contracts.clients)로 붙으므로 이미지 안에서 I4·I6 HTTP 경로가 돈다."""
import tempfile
from pathlib import Path

from boundary_contracts.clients import HttpReleaseReader, HttpTraceIngest

from .fakes import RecordingConsole, RecordingDispatcher
from .http_support import TOK, publish
from .ref_servers import ReferenceServer

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
