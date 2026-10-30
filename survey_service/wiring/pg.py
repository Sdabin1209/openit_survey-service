"""PostgreSQL 접속 래퍼(psycopg 3). SQLite 구현과 같은 SQL 본문을 쓸 수 있게 `?` 자리표시를 `%s`로 바꾸고, 끊긴 연결을 다시 연다.

- 접속 문자열(암호 포함 가능)은 이 모듈 밖으로 나가지 않는다. 오류·로그에는 예외 클래스 이름만 쓴다(StoreError 메시지 포함).
- 자동 커밋 연결이다. 트랜잭션은 호출자가 `BEGIN`/`COMMIT`/`ROLLBACK` 문장으로 연다(`BEGIN IMMEDIATE`는 `BEGIN`으로 바뀐다).
- 재접속 규칙: 연결이 죽어 있으면 트랜잭션 밖에서는 문장 앞에서 다시 열고, 문장 실행 중 연결이 끊기면(트랜잭션 밖) 새 연결로 한 번 다시 시도한다.
  트랜잭션 안에서 끊기면 다시 시도하지 않고 오류를 올린다: 서버는 열린 트랜잭션을 되돌렸고, COMMIT 중 끊긴 쓰기는 반영 여부를 알 수 없다(호출자가 UNKNOWN으로 다룬다).
  트랜잭션 밖 한 문장 재시도는 멱등 문장(ON CONFLICT, 같은 조건의 UPDATE/DELETE)에만 안전하다. 이 영역의 그런 문장은 모두 그렇다.
"""
from __future__ import annotations

import os
import re
import socket
import threading
import time
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

# 주 연결의 대기 상한(초). 설정으로 바꾼다(`configure`, db.open_stores). 근거는 docs/boundary.md §5: 정상일 때 문장 하나는 수 ms이고 턴 하나의 쓰기는 몇 문장이라
# 2초면 느린 디스크·잠깐의 잠금 대기는 견디고(libpq 접속 상한은 2초 미만이 의미 없다), 서버 턴 총 마감(기본 6초)과 gateway 상한(8.5초) 안에서 재접속 한 번까지 시도할 수 있다. 근거: ADR-0011.
DEFAULT_CONNECT_TIMEOUT = 2.0         # libpq의 connect_timeout은 2초 미만이 의미 없다
DEFAULT_STATEMENT_TIMEOUT = 2.0
DEFAULT_RECONNECT_BACKOFF = 1.0       # 접속에 실패한 뒤 이 시간 동안은 다시 접속을 시도하지 않고 바로 실패한다(턴 하나가 접속 시도 여러 번을 기다리지 않게)
_cfg = {"connect": DEFAULT_CONNECT_TIMEOUT, "statement": DEFAULT_STATEMENT_TIMEOUT, "probe": None, "backoff": DEFAULT_RECONNECT_BACKOFF}


def configure(*, connect_timeout: float | None = None, statement_timeout: float | None = None, probe_timeout: float | None = None,
              reconnect_backoff: float | None = None) -> None:
    """이후 만드는 PgConn의 기본 상한을 정한다(서비스 시작 때 한 번)."""
    if connect_timeout is not None:
        _cfg["connect"] = float(connect_timeout)
    if statement_timeout is not None:
        _cfg["statement"] = float(statement_timeout)
    if probe_timeout is not None:
        _cfg["probe"] = float(probe_timeout)
    if reconnect_backoff is not None:
        _cfg["backoff"] = float(reconnect_backoff)


DEFAULT_PROBE_TIMEOUT = 2.0  # /readyz의 DB 확인 상한 기본값(초). libpq의 connect_timeout 최소 단위가 2초라 이보다 작게 의미가 없다


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
        self.probe_timeout = _cfg["probe"] or DEFAULT_PROBE_TIMEOUT      # /readyz 확인용 상한(초)
        self.connect_timeout = _cfg["connect"]            # 주 연결의 접속 상한(초)
        self.reconnect_backoff = _cfg["backoff"]
        self._retry_after = 0.0                           # time.monotonic() 기준: 이 시각 전에는 재접속을 시도하지 않는다
        self.statement_timeout = _cfg["statement"]        # 문장 하나의 상한(초): 서버 쪽 statement_timeout + 클라이언트 쪽 강제 끊기(서버가 얼어도 끝난다)
        self._lock = threading.RLock()
        self._conn = self._connect(initial=True)

    def _params(self, connect_secs: int, stmt_seconds: float) -> dict:
        params = psycopg.conninfo.conninfo_to_dict(self._url)       # URL의 options(예: search_path)를 덮어쓰지 않고 이어 붙인다
        params["options"] = (params.get("options", "") + f" -c statement_timeout={int(stmt_seconds * 1000)}").strip()
        params["connect_timeout"] = connect_secs
        return params

    def _connect(self, initial: bool = False):
        if not initial and time.monotonic() < self._retry_after:        # 방금 접속에 실패했다: 또 기다리지 않고 바로 실패한다(빠른 실패)
            raise psycopg.OperationalError("reconnect backoff")
        try:
            c = psycopg.connect(autocommit=True, **self._params(max(1, int(self.connect_timeout + 0.999)), self.statement_timeout))
            self._retry_after = 0.0
            return c
        except Exception as e:                      # 메시지에 host/user가 들어갈 수 있어 클래스 이름만 남긴다
            if initial:
                raise StoreError(f"{self._what} 접속 실패: {type(e).__name__}") from None
            self._retry_after = time.monotonic() + self.reconnect_backoff
            raise psycopg.OperationalError(f"reconnect failed: {type(e).__name__}") from None     # 호출자는 드라이버 오류로 다룬다(StoreError/DB_ERROR로 바꾼다)

    @staticmethod
    def _abort(conn) -> None:
        """서버가 얼어 응답이 없을 때(docker pause 등) 서버 쪽 statement_timeout은 듣지 못한다. 소켓을 닫아 막힌 읽기를 끝낸다."""
        try:
            s = socket.socket(fileno=os.dup(conn.fileno()))
            try:
                s.shutdown(socket.SHUT_RDWR)
            finally:
                s.close()
        except Exception:
            pass

    def _exec(self, sql: str, args):
        """문장 하나를 상한 안에서 실행한다. statement_timeout + 1초 안에 안 끝나면 연결을 끊어 OperationalError로 끝낸다."""
        conn = self._conn
        timer = threading.Timer(self.statement_timeout + 1.0, self._abort, args=(conn,))
        timer.daemon = True
        timer.start()
        try:
            return conn.execute(translate(sql), tuple(args))
        finally:
            timer.cancel()

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
                cur = self._exec(sql, args)
            except psycopg.Error as e:
                lost = self._lost(e)
                if lost and not self._in_txn and not end:
                    self._conn = self._connect()
                    cur = self._exec(sql, args)      # 트랜잭션 밖의 멱등 문장: 새 연결로 한 번
                else:
                    if lost:
                        self._in_txn = False
                    raise
            if _BEGIN.match(sql):
                self._in_txn = True
            elif end:
                self._in_txn = False
            return cur

    def probe(self, table: str) -> None:
        """/readyz용 DB 확인. **이 객체의 연결·락을 쓰지 않는다**: 요청 처리나 outbox 전송이 연결을 쥐고 오래 기다리는 중이어도
        막히지 않고, 접속 시도가 `probe_timeout` 안에 끝난다(connect_timeout + 서버 쪽 statement_timeout).
        실패하면 드라이버 오류를 올린다(메시지에 접속 정보가 들어갈 수 있어 호출자가 클래스 이름만 쓴다)."""
        params = self._params(max(1, int(self.probe_timeout + 0.999)), self.probe_timeout)
        with psycopg.connect(autocommit=True, **params) as c:
            c.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()      # table은 코드가 정한 상수

    def kill_backend_pid(self) -> int:
        return self._conn.info.backend_pid
