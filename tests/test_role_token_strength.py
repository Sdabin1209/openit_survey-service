"""D03: 역할 토큰(kiosk, staff, result-reader, risk-source)에도 기기 토큰과 같은 최소 길이(16자)를 적용한다.

기대값의 출처: 워크스페이스 결정표(헤드 2026-10-29) '기기 토큰과 같은 최소 길이 규칙을 역할 토큰에도'. 기기 토큰의 16자 규칙은 service.py `_device_tokens`
(ADR-0016)에 이미 있다. 이 시험은 그 규칙이 네 역할 토큰 모두에 적용되는지와 오류에 토큰 값이 없는지를 본다.
"""
import pytest

from survey_service.service import App
from survey_service.settings import ConfigError, Settings

OK = {"SURVEY_API_TOKEN": "api-token-0123456", "SURVEY_STAFF_API_TOKEN": "staff-token-012345",
      "SURVEY_RESULT_READER_TOKEN": "result-token-01234", "SURVEY_RISK_SOURCE_TOKEN": "risk-token-012345"}


def _app(tmp_path):
    s = Settings(session_db=str(tmp_path / "s.db"), outbox_db=str(tmp_path / "o.db"), work_dir=str(tmp_path / "w"), model_backend="fake")
    return App(s, reader=object(), ingest=object())


def _env(monkeypatch, **over):
    for k, v in {**OK, **over}.items():
        monkeypatch.setenv(k, v)


def test_sixteen_char_role_tokens_are_accepted(tmp_path, monkeypatch):
    _env(monkeypatch, SURVEY_API_TOKEN="a" * 16, SURVEY_STAFF_API_TOKEN="b" * 16, SURVEY_RESULT_READER_TOKEN="c" * 16, SURVEY_RISK_SOURCE_TOKEN="d" * 16)
    _app(tmp_path)


@pytest.mark.parametrize("name", list(OK))
def test_short_role_token_is_rejected_without_echoing_it(tmp_path, monkeypatch, name):
    secret = "short-15-chars!"
    assert len(secret) == 15
    _env(monkeypatch, **{name: secret})
    with pytest.raises(ConfigError) as ei:
        _app(tmp_path)
    assert "16" in str(ei.value) and secret not in str(ei.value)
