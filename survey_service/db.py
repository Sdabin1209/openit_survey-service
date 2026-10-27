"""저장소 선택: `SURVEY_DATABASE_URL`이 있으면 PostgreSQL(세션, outbox, 알림 모두 한 데이터베이스), 없으면 기존 SQLite 파일.

접속 문자열은 `read_secret`(NAME_FILE 우선, 없으면 NAME)으로 읽고 이 모듈 밖에서는 `/readyz`의 종류 이름만 쓴다.
"""
from __future__ import annotations

from boundary_contracts import SecretError, read_secret

from . import pg
from .alerts import AlertStore, PgAlertStore
from .settings import ConfigError, Settings
from .store.sqlite import SqliteSessionStore
from .trace import Outbox
from .trace.pg_outbox import PgOutbox
from .store.postgres import PgSessionStore

URL_ENV = "SURVEY_DATABASE_URL"


def database_url() -> str:
    try:
        return read_secret(URL_ENV) or ""
    except SecretError as e:
        raise ConfigError(f"데이터베이스 접속 문자열: {e}") from None


def open_stores(settings: Settings, url: str | None = None):
    """(세션 저장소, outbox, 알림 저장소, 종류). 종류는 'postgresql' 또는 'sqlite'."""
    url = database_url() if url is None else url
    if url:
        pg.configure(connect_timeout=settings.db_connect_timeout_seconds, statement_timeout=settings.db_statement_timeout_seconds,
                     probe_timeout=settings.readyz_db_timeout_seconds)
        stores = PgSessionStore(url), PgOutbox(url), PgAlertStore(url)
        return (*stores, "postgresql")
    return SqliteSessionStore(settings.session_db), Outbox(settings.outbox_db), AlertStore(settings.alert_db_path()), "sqlite"
