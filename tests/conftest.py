import pytest


@pytest.fixture(autouse=True)
def _api_token(monkeypatch):
    """서비스 조립(App)은 공개 API 토큰을 환경 변수에서 읽는다(SURVEY_API_TOKEN)."""
    monkeypatch.setenv("SURVEY_API_TOKEN", "test-api-token-0123")
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "test-staff-token")
