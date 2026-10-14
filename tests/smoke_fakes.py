"""컨테이너 스모크 시험이 환경 변수로 꽂는 대역 팩토리(SURVEY_RELEASE_READER=tests.smoke_fakes:reader 등).
이미지에는 들어 있지 않고 스모크 스크립트가 tests/를 읽기 전용으로 붙인다."""
import tempfile
from pathlib import Path

from .fakes import FakeReleaseReader, FakeTraceIngest, RecordingConsole, RecordingDispatcher
from .sample_bank import sample_bank


def reader():
    r = FakeReleaseReader(Path(tempfile.mkdtemp(prefix="smoke-gate-")))
    r.add_release("rel-smoke", sample_bank(), turn_engine_version=__import__("turn_engine").ENGINE_VERSION,
                  model_runtime_version=__import__("model_runtime").RUNTIME_VERSION)
    r.set_pointer("rel-smoke")
    return r


def ingest():
    return FakeTraceIngest()


def console():
    return RecordingConsole()


def dispatcher():
    return RecordingDispatcher()
