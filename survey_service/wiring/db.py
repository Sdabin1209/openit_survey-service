"""저장소 선택: `SURVEY_DATABASE_URL`이 있으면 PostgreSQL(세션, outbox, 알림 모두 한 데이터베이스), 없으면 기존 SQLite 파일.

접속 문자열은 `read_secret`(NAME_FILE 우선, 없으면 NAME)으로 읽고 이 모듈 밖에서는 `/readyz`의 종류 이름만 쓴다.
"""
from __future__ import annotations

from boundary_contracts import SecretError, read_secret

from ..components.alert_book import AlertStore, PgAlertStore
from ..config import REAL_MONOTONIC
from ..settings import ConfigError, Settings
from ..components import session_store
from ..components.session_store import PgSessionStore, SqliteSessionStore
from ..components.trace_outbox import Outbox, PgOutbox

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
        session_store.configure(monotonic=REAL_MONOTONIC, connect_timeout=settings.db_connect_timeout_seconds, statement_timeout=settings.db_statement_timeout_seconds,
                     reconnect_backoff=settings.db_reconnect_backoff_seconds,
                     probe_timeout=settings.readyz_db_timeout_seconds)
        # 왜 주입하나: outbox·알림은 세션 저장소 컴포넌트를 import하지 못한다(컴포넌트끼리 import 금지). 연결 소유자는 session_store다.
        link = {"connect": session_store.PgConn, "driver_errors": session_store.DRIVER_ERRORS}
        store, outbox = PgSessionStore(url), PgOutbox(url, **link)
        store.outbox_gate = outbox          # 같은 데이터베이스의 outbox 표를 세션 commit과 같은 트랜잭션에 쓴다 (H02, ADR-0023)
        return store, outbox, PgAlertStore(url, **link), "postgresql"
    # 왜: SQLite도 outbox 표를 세션 DB 파일에 둔다. 세션 commit과 같은 트랜잭션에 쓰려면 같은 파일이어야 한다(H02, ADR-0023).
    # settings.outbox_db(SURVEY_OUTBOX_DB)는 더 쓰지 않는다. 그 파일에 남은 미전송 행은 옮기지 않는다(운영은 PostgreSQL, SQLite는 개발·시험).
    store, outbox = SqliteSessionStore(settings.session_db), Outbox(settings.session_db)
    store.outbox_gate = outbox
    return store, outbox, AlertStore(settings.alert_db_path()), "sqlite"
