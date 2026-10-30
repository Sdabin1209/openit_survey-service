"""I4·I6 연결: boundary_contracts.clients의 HTTP 클라이언트를 설정(환경 변수)으로 만든다(HTTP.md, D49).

토큰 값은 비밀 파일(NAME_FILE) 또는 개발용 환경 변수(NAME)로 받는다(D84, boundary_contracts.read_secret). 설정에는 토큰이 든 환경 변수의 '이름'과 base URL만 둔다.
이 모듈은 서비스 진입점 쪽이라 환경 변수를 읽는다(CONVENTIONS 7절). 클라이언트 생성자는 환경을 읽지 않는다.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from boundary_contracts import SecretError, read_secret
from openit_link.clients import HttpReleaseReader, HttpTraceIngest

from ..settings import ConfigError


def _make(cls, env: Optional[Mapping], prefix: str, what: str):
    env = os.environ if env is None else env
    url = env.get(f"{prefix}_URL", "")
    if not url:
        raise ConfigError(f"{prefix}_URL이 없다")
    token_env = env.get(f"{prefix}_TOKEN_ENV", f"{prefix}_TOKEN")
    try:
        token = read_secret(token_env, environ=env)
    except SecretError as e:
        raise ConfigError(f"{what} 토큰: {e}") from None
    if not token:
        raise ConfigError(f"{what} 토큰이 {token_env}_FILE(파일) 또는 환경 변수 {token_env}에 없다")
    try:
        timeout = float(env.get("SURVEY_HTTP_TIMEOUT_SECONDS", "10"))
        return cls(url, token, timeout)
    except ValueError as e:
        raise ConfigError("SURVEY_HTTP_TIMEOUT_SECONDS 또는 URL 형식이 잘못됐다") from e


def http_release_reader(env: Optional[Mapping] = None) -> HttpReleaseReader:
    """SURVEY_RELEASE_READER_URL, 토큰은 SURVEY_RELEASE_READER_TOKEN_ENV가 가리키는 환경 변수(기본 SURVEY_RELEASE_READER_TOKEN)."""
    return _make(HttpReleaseReader, env, "SURVEY_RELEASE_READER", "릴리스 읽기")


def http_trace_ingest(env: Optional[Mapping] = None) -> HttpTraceIngest:
    """SURVEY_TRACE_INGEST_URL, 토큰은 SURVEY_TRACE_INGEST_TOKEN_ENV가 가리키는 환경 변수(기본 SURVEY_TRACE_INGEST_TOKEN)."""
    return _make(HttpTraceIngest, env, "SURVEY_TRACE_INGEST", "Trace 전송")
