"""PostgreSQL Session Store. SQLite 구현과 같은 SQL·같은 의미이고 같은 적합성 시험(tests/test_store.py)을 통과한다.

차이는 연결(`PgConn`: 끊기면 다시 연다, 접속 문자열 비노출), 스키마 방언(실수 열, 자동 증가 없음), 행 잠금(`FOR UPDATE`)뿐이다.
문서는 SQLite와 같이 TEXT(JSON)로 저장한다. 낙관적 revision 검사와 행 잠금이 쓰기 충돌을 막는다.
"""
from __future__ import annotations

from ..errors import StoreError
from ..pg import DRIVER_ERRORS, PgConn, pg_ddl
from .sqlite import _SCHEMA, SqliteSessionStore


class PgSessionStore(SqliteSessionStore):
    _ERRORS = DRIVER_ERRORS
    _FOR_UPDATE = " FOR UPDATE"
    KIND = "postgresql"

    def _open(self, url: str) -> None:
        self._db = PgConn(url, "세션 DB")
        self._db.script(pg_ddl(_SCHEMA))
        self._db.execute("CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(status, updated_at)")

    def ping(self) -> None:
        """/readyz용. 주 연결과 락을 쓰지 않는 별도 짧은 연결로 확인한다(DB가 멈춰도 상한 안에 끝난다)."""
        try:
            self._db.probe("sessions")
        except self._ERRORS as e:
            raise StoreError(f"세션 DB를 읽을 수 없다: {type(e).__name__}") from None
