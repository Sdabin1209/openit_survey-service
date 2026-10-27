"""PostgreSQL 접속 래퍼(psycopg 3). SQLite 구현과 같은 SQL 본문을 쓸 수 있게 `?` 자리표시를 `%s`로 바꾸고, 끊긴 연결을 다시 연다.

- 접속 문자열(암호 포함 가능)은 이 모듈 밖으로 나가지 않는다. 오류·로그에는 예외 클래스 이름만 쓴다(StoreError 메시지 포함).
- 자동 커밋 연결이다. 트랜잭션은 호출자가 `BEGIN`/`COMMIT`/`ROLLBACK` 문장으로 연다(`BEGIN IMMEDIATE`는 `BEGIN`으로 바뀐다).
- 재접속 규칙: 연결이 죽어 있으면 트랜잭션 밖에서는 문장 앞에서 다시 열고, 문장 실행 중 연결이 끊기면(트랜잭션 밖) 새 연결로 한 번 다시 시도한다.
  트랜잭션 안에서 끊기면 다시 시도하지 않고 오류를 올린다: 서버는 열린 트랜잭션을 되돌렸고, COMMIT 중 끊긴 쓰기는 반영 여부를 알 수 없다(호출자가 UNKNOWN으로 다룬다).
  트랜잭션 밖 한 문장 재시도는 멱등 문장(ON CONFLICT, 같은 조건의 UPDATE/DELETE)에만 안전하다. 이 영역의 그런 문장은 모두 그렇다.
"""
from __future__ import annotations

import re
import threading
from typing import Any

from .errors import StoreError

_BEGIN = re.compile(r"^\s*BEGIN\b", re.I)
_END = re.compile(r"^\s*(COMMIT|ROLLBACK)\b", re.I)
DRIVER_ERRORS: tuple

try:
    import psycopg
    import psycopg.conninfo
    DRIVER_ERRORS = (psycopg.Error,)
except ImportError:          # 드라이버가 없으면 SQLite만 쓴다
    psycopg = None           # type: ignore[assignment]
    DRIVER_ERRORS = ()

DEFAULT_PROBE_TIMEOUT = 2.0  # /readyz의 DB 확인 상한 기본값(초). libpq의 connect_timeout 최소 단위가 2초라 이보다 작게 의미가 없다
_LOCK_KEY = 7264002          # 스키마 생성용 advisory lock 키(이 영역 고정값)


def translate(sql: str) -> str:
    """SQLite 방언의 자리표시(`?`)와 `BEGIN IMMEDIATE`를 PostgreSQL로. 리터럴의 `%`는 이스케이프한다."""
    sql = sql.replace("%", "%%").replace("?", "%s")
    return re.sub(r"^\s*BEGIN\s+IMMEDIATE\b", "BEGIN", sql, flags=re.I)


class PgConn:
    KIND = "postgresql"

    def __init__(self, url: str, what: str = "데이터베이스") -> None:
        if psycopg is None:
            raise StoreError(f"{what}: psycopg 드라이버가 없다")
        self._url, self._what = url, what          # 어디에도 출력하지 않는다
        self._in_txn = False
        self.probe_timeout = DEFAULT_PROBE_TIMEOUT      # /readyz 확인용 상한(초). open_stores가 설정 값으로 덮어쓴다
        self._lock = threading.RLock()
        self._conn = self._connect(initial=True)

    def _connect(self, initial: bool = False):
        try:
            return psycopg.connect(self._url, autocommit=True, connect_timeout=10)
        except Exception as e:                      # 메시지에 host/user가 들어갈 수 있어 클래스 이름만 남긴다
            if initial:
                raise StoreError(f"{self._what} 접속 실패: {type(e).__name__}") from None
            raise psycopg.OperationalError(f"reconnect failed: {type(e).__name__}") from None     # 호출자는 드라이버 오류로 다룬다(StoreError/DB_ERROR로 바꾼다)

    def _alive(self) -> bool:
        return not (self._conn.closed or self._conn.broken)

    def _lost(self, e: Exception) -> bool:
        """연결이 끊겨 이 연결을 더 쓸 수 없는 오류인가(서버가 연결을 끊은 오류 코드 08·57P 포함)."""
        if not isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError)):
            return False
        code = getattr(e, "sqlstate", None) or ""
        if not self._alive() or code.startswith(("08", "57P")) or isinstance(e, psycopg.InterfaceError):
            try:
                self._conn.close()
            except Exception:
                pass
            return True
        return False

    def ensure(self) -> None:
        if not self._alive():
            self._in_txn = False
            self._conn = self._connect()

    @property
    def closed(self) -> bool:
        return self._conn.closed

    def close(self) -> None:
        self._conn.close()

    def execute(self, sql: str, args: Any = ()):
        with self._lock:
            end = bool(_END.match(sql))
            if not self._alive():
                if self._in_txn:                    # 열린 트랜잭션은 서버가 되돌렸다. 다시 시도하지 않는다
                    self._in_txn = False
                    if end and sql.strip().upper().startswith("ROLLBACK"):
                        return None
                    raise psycopg.OperationalError("connection lost")
                self._conn = self._connect()
            try:
                cur = self._conn.execute(translate(sql), tuple(args))
            except psycopg.Error as e:
                lost = self._lost(e)
                if lost and not self._in_txn and not end:
                    self._conn = self._connect()
                    cur = self._conn.execute(translate(sql), tuple(args))      # 트랜잭션 밖의 멱등 문장: 새 연결로 한 번
                else:
                    if lost:
                        self._in_txn = False
                    raise
            if _BEGIN.match(sql):
                self._in_txn = True
            elif end:
                self._in_txn = False
            return cur

    def script(self, ddl: str, advisory: bool = True) -> None:
        """스키마를 멱등으로 만든다(advisory lock으로 서버와 잡이 동시에 떠도 안전)."""
        if advisory:
            self.execute("SELECT pg_advisory_lock(%s)" % _LOCK_KEY)
        try:
            self._conn.execute(ddl)
        finally:
            if advisory:
                self.execute("SELECT pg_advisory_unlock(%s)" % _LOCK_KEY)

    def probe(self, table: str) -> None:
        """/readyz용 DB 확인. **이 객체의 연결·락을 쓰지 않는다**: 요청 처리나 outbox 전송이 연결을 쥐고 오래 기다리는 중이어도
        막히지 않고, 접속 시도가 `probe_timeout` 안에 끝난다(connect_timeout + 서버 쪽 statement_timeout).
        실패하면 드라이버 오류를 올린다(메시지에 접속 정보가 들어갈 수 있어 호출자가 클래스 이름만 쓴다)."""
        secs = max(1, int(self.probe_timeout + 0.999))
        params = psycopg.conninfo.conninfo_to_dict(self._url)       # URL의 options(예: search_path)를 덮어쓰지 않고 이어 붙인다
        params["options"] = (params.get("options", "") + f" -c statement_timeout={int(self.probe_timeout * 1000)}").strip()
        params["connect_timeout"] = secs
        with psycopg.connect(autocommit=True, **params) as c:
            c.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()      # table은 코드가 정한 상수

    def kill_backend_pid(self) -> int:
        return self._conn.info.backend_pid


def pg_ddl(ddl: str) -> str:
    """SQLite용 DDL을 PostgreSQL로: 자동 증가 키와 실수 열."""
    return (ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
               .replace(" REAL", " DOUBLE PRECISION"))
