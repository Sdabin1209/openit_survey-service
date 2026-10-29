"""D84: 비밀은 NAME_FILE 우선, 없으면 NAME. 값은 로그·/readyz·오류·Trace에 나오지 않는다."""
import json
import logging

import pytest

from survey_service.clients import http_release_reader, http_trace_ingest
from survey_service.service import App
from survey_service.settings import ConfigError, Settings

API, STAFF = "api-secret-AAA111", "staff-secret-BBB222"
ENV_NAMES = ("SURVEY_API_TOKEN", "SURVEY_STAFF_API_TOKEN", "SURVEY_INFERENCE_TOKEN", "SURVEY_VAPID_PRIVATE_KEY")


def _clear(monkeypatch):
    for n in ENV_NAMES:
        monkeypatch.delenv(n, raising=False)
        monkeypatch.delenv(n + "_FILE", raising=False)


def _settings(tmp_path):
    return Settings(session_db=str(tmp_path / "s.db"), outbox_db=str(tmp_path / "o.db"), work_dir=str(tmp_path / "w"),
                    model_backend="fake")


def _write(tmp_path, name, value):
    p = tmp_path / name
    p.write_text(value + "\n")
    return str(p)


def _app(tmp_path):
    return App(_settings(tmp_path), reader=object(), ingest=object())


def test_file_wins_and_sources_reported_without_values(tmp_path, monkeypatch, caplog):
    _clear(monkeypatch)
    monkeypatch.setenv("SURVEY_API_TOKEN_FILE", _write(tmp_path, "api", API))
    monkeypatch.setenv("SURVEY_API_TOKEN", "ignored-env-value-ZZZ")
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", STAFF)
    caplog.set_level(logging.DEBUG)
    app = _app(tmp_path)
    ok, body = app.readiness()
    assert body["secrets"]["api_token"] == "file" and body["secrets"]["staff_api_token"] == "env"
    assert body["secrets"]["vapid_private_key"] == "unset"
    blob = json.dumps(body, ensure_ascii=False) + caplog.text
    for v in (API, STAFF, "ignored-env-value-ZZZ", str(tmp_path / "api")):
        assert v not in blob
    for f in tmp_path.rglob("*.db"):                       # 저장소·Trace outbox 파일에도 비밀이 없다
        raw = f.read_bytes()
        assert API.encode() not in raw and STAFF.encode() not in raw


def test_missing_file_error_has_no_value(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SURVEY_API_TOKEN_FILE", str(tmp_path / "nope"))
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", STAFF)
    with pytest.raises(ConfigError) as ei:
        _app(tmp_path)
    assert STAFF not in str(ei.value)


def test_empty_file_rejected(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SURVEY_API_TOKEN_FILE", _write(tmp_path, "api", ""))
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", STAFF)
    with pytest.raises(ConfigError):
        _app(tmp_path)


def test_unset_is_config_error_naming_file_and_env(tmp_path, monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(ConfigError) as ei:
        _app(tmp_path)
    assert "SURVEY_STAFF_API_TOKEN_FILE" in str(ei.value)       # 키오스크 토큰은 2.x 호환이라 없어도 된다(ADR-0031). 직원 토큰이 없을 때 이름을 알려 준다


def test_same_tokens_still_rejected(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SURVEY_API_TOKEN_FILE", _write(tmp_path, "a", API))
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN_FILE", _write(tmp_path, "b", API))
    with pytest.raises(ConfigError) as ei:
        _app(tmp_path)
    assert API not in str(ei.value)


def test_clients_token_env_indirection_with_file(tmp_path):
    tok = _write(tmp_path, "rr", "reader-secret-CCC333")
    env = {"SURVEY_RELEASE_READER_URL": "http://x", "SURVEY_RELEASE_READER_TOKEN_ENV": "MY_RR", "MY_RR_FILE": tok}
    assert http_release_reader(env) is not None
    env2 = {"SURVEY_TRACE_INGEST_URL": "http://x", "SURVEY_TRACE_INGEST_TOKEN": "t-DDD444"}
    assert http_trace_ingest(env2) is not None
    with pytest.raises(ConfigError) as ei:
        http_trace_ingest({"SURVEY_TRACE_INGEST_URL": "http://x"})
    assert "SURVEY_TRACE_INGEST_TOKEN_FILE" in str(ei.value)
    with pytest.raises(ConfigError) as ei:
        http_release_reader({"SURVEY_RELEASE_READER_URL": "http://x", "SURVEY_RELEASE_READER_TOKEN_FILE": str(tmp_path / "gone")})
    assert "reader-secret" not in str(ei.value)
