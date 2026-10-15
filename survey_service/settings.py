"""설정은 환경 변수로만 받는다. 읽는 곳은 서비스 진입점뿐이다(CONVENTIONS 7절)."""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional


class ConfigError(Exception):
    pass


def _int(env: Mapping, key: str, default: int, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{key}는 정수여야 한다") from None
    if v < minimum:
        raise ConfigError(f"{key}는 {minimum} 이상이어야 한다")
    return v


def _float(env: Mapping, key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        raise ConfigError(f"{key}는 숫자여야 한다") from None
    if v <= 0:
        raise ConfigError(f"{key}는 0보다 커야 한다")
    return v


@dataclass(frozen=True)
class Settings:
    port: int = 8080
    session_db: str = "/data/sessions.db"
    outbox_db: str = "/data/outbox.db"
    work_dir: str = "/data/releases"
    release_reader: str = ""        # I4 구현 팩토리 "모듈:함수"
    trace_ingest: str = ""          # I6 구현 팩토리
    model_backend: str = ""         # 예: fake
    staff_console: str = ""         # 선택 팩토리. 없으면 알림 전달은 실패로 기록된다
    result_dispatcher: str = ""     # 선택 팩토리. 없으면 결과 전달은 실패로 기록된다
    release_poll_seconds: float = 5.0
    # HTTP 클라이언트(release_reader/trace_ingest가 'http'일 때): 토큰은 값이 아니라 '환경 변수 이름'으로 받는다
    release_reader_url: str = ""
    release_reader_token_env: str = "SURVEY_RELEASE_READER_TOKEN"
    trace_ingest_url: str = ""
    trace_ingest_token_env: str = "SURVEY_TRACE_INGEST_TOKEN"
    http_timeout_seconds: float = 10.0
    # 공개 API(키오스크 프론트, PUBLIC_API.md). 토큰은 값이 아니라 '환경 변수 이름'으로 받는다
    api_token_env: str = "SURVEY_API_TOKEN"
    staff_api_token_env: str = "SURVEY_STAFF_API_TOKEN"    # 직원 개입용 별도 토큰(역할 staff)
    api_max_inflight: int = 32
    api_max_utterance_chars: int = 1000
    session_idle_seconds: int = 300          # 0이면 유휴 만료를 하지 않는다
    # 모델 런타임을 HTTP 경계 뒤에 둘 때(추론 컨테이너): 비우면 프로세스 안에서 적재한다
    inference_url: str = ""
    inference_token_env: str = "SURVEY_INFERENCE_TOKEN"
    inference_timeout_seconds: float = 4.0      # 호출 하나(parse 또는 generate)의 읽기 타임아웃. 프론트 타임아웃 10초 안에 두 호출이 들어가게
    inference_load_timeout_seconds: float = 600.0   # 릴리스 적재(모델 로딩)를 기다리는 시간
    inference_retries: int = 1
    keep_releases: int = 2
    export_interval_seconds: float = 1.0
    export_backoff_max_seconds: float = 60.0
    outbox_keep_sent_hours: int = 24
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        s = cls(
            port=_int(env, "SURVEY_PORT", 8080, 1), session_db=env.get("SURVEY_SESSION_DB", cls.session_db),
            outbox_db=env.get("SURVEY_OUTBOX_DB", cls.outbox_db), work_dir=env.get("SURVEY_WORK_DIR", cls.work_dir),
            release_reader=env.get("SURVEY_RELEASE_READER", ""), trace_ingest=env.get("SURVEY_TRACE_INGEST", ""),
            model_backend=env.get("SURVEY_MODEL_BACKEND", ""), staff_console=env.get("SURVEY_STAFF_CONSOLE", ""),
            result_dispatcher=env.get("SURVEY_RESULT_DISPATCHER", ""),
            release_reader_url=env.get("SURVEY_RELEASE_READER_URL", ""),
            release_reader_token_env=env.get("SURVEY_RELEASE_READER_TOKEN_ENV", "SURVEY_RELEASE_READER_TOKEN"),
            trace_ingest_url=env.get("SURVEY_TRACE_INGEST_URL", ""),
            trace_ingest_token_env=env.get("SURVEY_TRACE_INGEST_TOKEN_ENV", "SURVEY_TRACE_INGEST_TOKEN"),
            api_token_env=env.get("SURVEY_API_TOKEN_ENV", "SURVEY_API_TOKEN"),
            staff_api_token_env=env.get("SURVEY_STAFF_API_TOKEN_ENV", "SURVEY_STAFF_API_TOKEN"),
            api_max_inflight=_int(env, "SURVEY_API_MAX_INFLIGHT", 32, 1),
            api_max_utterance_chars=_int(env, "SURVEY_API_MAX_UTTERANCE_CHARS", 1000, 1),
            session_idle_seconds=_int(env, "SURVEY_SESSION_IDLE_SECONDS", 300, 0),
            inference_url=env.get("SURVEY_INFERENCE_URL", ""),
            inference_token_env=env.get("SURVEY_INFERENCE_TOKEN_ENV", "SURVEY_INFERENCE_TOKEN"),
            inference_timeout_seconds=_float(env, "SURVEY_INFERENCE_TIMEOUT_SECONDS", 4.0),
            inference_load_timeout_seconds=_float(env, "SURVEY_INFERENCE_LOAD_TIMEOUT_SECONDS", 600.0),
            inference_retries=_int(env, "SURVEY_INFERENCE_RETRIES", 1, 0),
            http_timeout_seconds=_float(env, "SURVEY_HTTP_TIMEOUT_SECONDS", 10.0),
            release_poll_seconds=_float(env, "SURVEY_RELEASE_POLL_SECONDS", 5.0), keep_releases=_int(env, "SURVEY_KEEP_RELEASES", 2),
            export_interval_seconds=_float(env, "SURVEY_EXPORT_INTERVAL_SECONDS", 1.0),
            export_backoff_max_seconds=_float(env, "SURVEY_EXPORT_BACKOFF_MAX_SECONDS", 60.0),
            outbox_keep_sent_hours=_int(env, "SURVEY_OUTBOX_KEEP_SENT_HOURS", 24, 1), log_level=env.get("SURVEY_LOG_LEVEL", "INFO").upper())
        missing = [n for n, v in (("SURVEY_RELEASE_READER", s.release_reader), ("SURVEY_TRACE_INGEST", s.trace_ingest),
                                  ("SURVEY_MODEL_BACKEND", s.model_backend or s.inference_url)) if not v]
        for name, spec, url in (("SURVEY_RELEASE_READER", s.release_reader, s.release_reader_url),
                                ("SURVEY_TRACE_INGEST", s.trace_ingest, s.trace_ingest_url)):
            if spec == "http" and not url:
                missing.append(name + "_URL")
        if missing:
            raise ConfigError("필수 환경 변수가 없다: " + ", ".join(missing))
        return s


def load_factory(spec: str) -> Callable[[], Any]:
    """'모듈:함수' 문자열을 함수로. 'http'는 boundary_contracts의 HTTP 클라이언트를 만드는 기본 팩토리를 뜻한다
    (SURVEY_RELEASE_READER=http 이면 ...:http_release_reader, SURVEY_TRACE_INGEST=http 이면 ...:http_trace_ingest)."""
    if spec == "http":
        raise ConfigError("'http'는 읽는 쪽(서비스 조립)이 역할에 맞는 팩토리로 바꿔야 한다")
    if ":" not in spec:
        raise ConfigError(f"'모듈:함수' 형식이어야 한다: {spec!r}")
    mod, _, fn = spec.partition(":")
    try:
        return getattr(importlib.import_module(mod), fn)
    except (ImportError, AttributeError) as e:
        raise ConfigError(f"팩토리를 불러올 수 없다: {spec} ({type(e).__name__})") from e
