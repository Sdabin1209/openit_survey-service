"""PostgreSQL outbox. SQLite 구현(outbox.py)과 같은 SQL·같은 의미이고 같은 적합성 시험을 통과한다."""
from __future__ import annotations

from ..pg import DRIVER_ERRORS, PgConn, pg_ddl
from .outbox import _SCHEMA, Outbox


class PgOutbox(Outbox):
    _ERRORS = DRIVER_ERRORS
    KIND = "postgresql"

    def _open(self, url: str) -> None:
        self._db = PgConn(url, "outbox")
        self._db.script(pg_ddl(_SCHEMA))
