"""I4·I6 연결: boundary_contracts.clients의 HTTP 클라이언트를 설정(환경 변수)으로 만든다(HTTP.md, D49).

토큰 값은 비밀 파일(NAME_FILE) 또는 개발용 환경 변수(NAME)로 받는다(D84, boundary_contracts.read_secret). 설정에는 토큰이 든 환경 변수의 '이름'과 base URL만 둔다.
이 모듈은 서비스 진입점 쪽이라 환경 변수를 읽는다(CONVENTIONS 7절). 클라이언트 생성자는 환경을 읽지 않는다.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from boundary_contracts import SecretError, read_secret
from boundary_contracts.clients import egress_proxy_from
from openit_link.clients import HttpReleaseReader, HttpTraceIngest

from ..settings import ConfigError


TAR_TIMEOUT_SECONDS = 300.0      # R10.3: tar 묶음을 주고받는 호출(I2·I3 게시·내려받기, I4 번들)은 호출자가 timeout 300초를 넘긴다(시작값·측정 안 함). 그 밖의 호출은 SURVEY_HTTP_TIMEOUT_SECONDS(기본 10초)


class _BundleReader:
    """I4 읽기 클라이언트. 번들(tar) 내려받기만 300초 시간 한도의 클라이언트로 하고 나머지는 모두 기본 시간 한도 클라이언트로 한다(R10.3)."""

    def __init__(self, reader, bundle_reader) -> None:
        self._reader, self._bundle = reader, bundle_reader

    def load_bundle(self, release_id, *, dest):
        return self._bundle.load_bundle(release_id, dest=dest)

    def __getattr__(self, name):
        return getattr(self._reader, name)


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
        egress = egress_proxy_from(env)      # 영역 밖 호출은 자기 영역 게이트웨이의 CONNECT 터널로만 나간다(R1.8). 없으면 직접(배치가 게이트웨이를 주지 않은 구성)
        client = cls(url, token, timeout, egress_proxy=egress)
        if cls is HttpReleaseReader:
            return _BundleReader(client, cls(url, token, TAR_TIMEOUT_SECONDS, egress_proxy=egress))
        return client
    except ValueError as e:
        raise ConfigError("SURVEY_HTTP_TIMEOUT_SECONDS 또는 URL 형식이 잘못됐다") from e


def http_release_reader(env: Optional[Mapping] = None) -> HttpReleaseReader:
    """SURVEY_RELEASE_READER_URL, 토큰은 SURVEY_RELEASE_READER_TOKEN_ENV가 가리키는 환경 변수(기본 SURVEY_RELEASE_READER_TOKEN)."""
    return _make(HttpReleaseReader, env, "SURVEY_RELEASE_READER", "릴리스 읽기")


def http_trace_ingest(env: Optional[Mapping] = None) -> HttpTraceIngest:
    """SURVEY_TRACE_INGEST_URL, 토큰은 SURVEY_TRACE_INGEST_TOKEN_ENV가 가리키는 환경 변수(기본 SURVEY_TRACE_INGEST_TOKEN)."""
    return _make(HttpTraceIngest, env, "SURVEY_TRACE_INGEST", "Trace 전송")
