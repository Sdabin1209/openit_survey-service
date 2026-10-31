import pytest


@pytest.fixture(autouse=True)
def _api_token(monkeypatch):
    """서비스 조립(App)은 공개 API 토큰을 환경 변수에서 읽는다(SURVEY_API_TOKEN)."""
    monkeypatch.setenv("SURVEY_API_TOKEN", "test-api-token-0123")
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "test-staff-token")


@pytest.fixture(autouse=True)
def _injected_clock_and_timer_for_pg():
    """PgConn은 시계와 시한 타이머를 주입받아야 만들어진다(ADR-0050). 시험은 가짜를 넣는다: 재접속 유예는 가짜 단조 시계(시험이 흘릴 때만 간다), 문장 강제 끊기 타이머는 fire()할 때만 실행."""
    from survey_service.components import session_store
    from .timing import FakeClock, FakeTimer
    session_store.configure(monotonic=FakeClock(), timer=FakeTimer)
    yield
