"""PostgreSQL Session Store. SQLite 구현과 같은 SQL·같은 의미이고 같은 적합성 시험(tests/test_store.py)을 통과한다.

차이는 연결(`PgConn`: 끊기면 다시 연다, 접속 문자열 비노출), 스키마 방언(실수 열, 자동 증가 없음), 행 잠금(`FOR UPDATE`)뿐이다.
문서는 SQLite와 같이 TEXT(JSON)로 저장한다. 낙관적 revision 검사와 행 잠금이 쓰기 충돌을 막는다.
"""
from __future__ import annotations

from ..errors import StoreError
from ..migrate import migrate
from ..pg import DRIVER_ERRORS, PgConn
from .sqlite import SqliteSessionStore


class PgSessionStore(SqliteSessionStore):
    _ERRORS = DRIVER_ERRORS
    _FOR_UPDATE = " FOR UPDATE"
    KIND = "postgresql"
    _NEXT_SEQ = "nextval('sessions_update_seq')"
    _SEQ_LOCK_KEY = 7264003        # update_seq 번호 부여를 직렬화하는 advisory lock 키(이 영역 고정값, 근거 ADR-0034)

    def _number_lock(self) -> None:
        # 번호를 받는 쓰기 트랜잭션은 커밋(또는 롤백)까지 이 잠금을 쥔다: 다음 쓰기는 앞 쓰기가 커밋한 뒤에야 번호를 받아 번호 순서 = 커밋 순서가 된다.
        # 이어 읽는 쪽(커서)이 늦게 커밋된 작은 번호를 건너뛰지 않는다. 트랜잭션 수준이라 연결이 죽어도 서버가 풀어 준다.
        self._db.execute("SELECT pg_advisory_xact_lock(?)", (self._SEQ_LOCK_KEY,)).fetchall()

    def _open(self, url: str) -> None:
        migrate("postgresql", url)
        self._db = PgConn(url, "세션 DB")

    def ping(self) -> None:
        """/readyz용. 주 연결과 락을 쓰지 않는 별도 짧은 연결로 확인한다(DB가 멈춰도 상한 안에 끝난다)."""
        try:
            self._db.probe("sessions")
        except self._ERRORS as e:
            raise StoreError(f"세션 DB를 읽을 수 없다: {type(e).__name__}") from None
