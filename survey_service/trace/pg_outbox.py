"""PostgreSQL outbox. SQLite 구현(outbox.py)과 같은 SQL·같은 의미이고 같은 적합성 시험을 통과한다."""
from __future__ import annotations

from ..errors import StoreError
from ..migrate import migrate
from ..pg import DRIVER_ERRORS, PgConn
from .outbox import Outbox


class PgOutbox(Outbox):
    _ERRORS = DRIVER_ERRORS
    KIND = "postgresql"

    def _open(self, url: str) -> None:
        migrate("postgresql", url)
        self._db = PgConn(url, "outbox")

    def ping(self) -> None:
        """/readyz용. 주 연결과 락을 쓰지 않는 별도 짧은 연결로 확인한다(전송 스레드가 연결을 쥐고 있어도 막히지 않는다)."""
        try:
            self._db.probe("outbox")
        except self._ERRORS as e:
            raise StoreError(f"outbox를 읽을 수 없다: {type(e).__name__}") from None
