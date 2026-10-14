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
            release_poll_seconds=_float(env, "SURVEY_RELEASE_POLL_SECONDS", 5.0), keep_releases=_int(env, "SURVEY_KEEP_RELEASES", 2),
            export_interval_seconds=_float(env, "SURVEY_EXPORT_INTERVAL_SECONDS", 1.0),
            export_backoff_max_seconds=_float(env, "SURVEY_EXPORT_BACKOFF_MAX_SECONDS", 60.0),
            outbox_keep_sent_hours=_int(env, "SURVEY_OUTBOX_KEEP_SENT_HOURS", 24, 1), log_level=env.get("SURVEY_LOG_LEVEL", "INFO").upper())
        missing = [n for n, v in (("SURVEY_RELEASE_READER", s.release_reader), ("SURVEY_TRACE_INGEST", s.trace_ingest),
                                  ("SURVEY_MODEL_BACKEND", s.model_backend)) if not v]
        if missing:
            raise ConfigError("필수 환경 변수가 없다: " + ", ".join(missing))
        return s


def load_factory(spec: str) -> Callable[[], Any]:
    """'모듈:함수' 문자열을 함수로. 이웃 영역이 배포하는 SDK의 팩토리를 설정으로 꽂는다."""
    if ":" not in spec:
        raise ConfigError(f"'모듈:함수' 형식이어야 한다: {spec!r}")
    mod, _, fn = spec.partition(":")
    try:
        return getattr(importlib.import_module(mod), fn)
    except (ImportError, AttributeError) as e:
        raise ConfigError(f"팩토리를 불러올 수 없다: {spec} ({type(e).__name__})") from e
