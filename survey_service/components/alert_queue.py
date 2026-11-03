"""직원 알림 재시도 큐(L14, ADR-0058). 입력: 저장하지 못한 알림(세션, 턴, 종류, 규칙, 정지 사유) -> 출력: 다시 시도할 알림 목록.

알림 DB에 알림을 쓰지 못했을 때 세션 DB(Trace outbox와 같은 데이터베이스)에 한 줄 남긴다. 전달은 서비스의 재시도 스레드가 한다(턴 처리와 분리, D16).
(세션, 턴, 종류)가 유일하다: 같은 알림을 두 번 넣어도 한 줄이다.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional

from boundary_contracts import BoundaryError
from ..migrate import migrate


class StoreError(BoundaryError):
    """저장소를 쓸 수 없다. 컴포넌트가 다른 모듈의 오류 클래스를 import하지 않으므로 같은 뜻의 클래스를 둔다(한계 L2)."""

    def __init__(self, message: str = "저장소를 쓸 수 없다") -> None:
        super().__init__("UNAVAILABLE", message, {"reason": "STORE_ERROR"})


@dataclass
class QueuedAlert:
    id: int
    session_id: str
    turn_id: Optional[str]
    kind: str
    rule_id: Optional[str]
    pause_reason: Optional[str]
    attempts: int


def _migrate(kind: str, target: str) -> None:
    try:
        migrate(kind, target)
    except BoundaryError as e:
        raise StoreError(e.message) from None


class AlertQueue:
    _ERRORS: tuple = (sqlite3.Error,)

    def __init__(self, path: str, *, clock) -> None:
        self._lock = threading.RLock()
        self._clock = clock        # 시각(초). 주입한다(ADR-0050)
        try:
            self._open(path)
        except self._ERRORS as e:
            raise StoreError(f"알림 큐를 열 수 없다: {type(e).__name__}") from e

    def _open(self, path: str) -> None:
        _migrate("sqlite", path)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def enqueue(self, session_id: str, turn_id: Optional[str], kind: str, rule_id: Optional[str], pause_reason: Optional[str]) -> bool:
        """처음 넣으면 True, 이미 있으면 False."""
        try:
            with self._lock:
                cur = self._db.execute("INSERT INTO alert_queue(session_id, turn_id, kind, rule_id, pause_reason, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                                       (session_id, turn_id or "", kind, rule_id, pause_reason, self._clock()))
                return cur.rowcount == 1
        except self._ERRORS as e:
            raise StoreError(f"알림 큐에 넣을 수 없다: {type(e).__name__}") from e

    def due(self, limit: int = 50) -> list[QueuedAlert]:
        try:
            with self._lock:
                rows = self._db.execute("SELECT id, session_id, turn_id, kind, rule_id, pause_reason, attempts FROM alert_queue WHERE next_attempt_at<=? ORDER BY id LIMIT ?",
                                        (self._clock(), limit)).fetchall()
        except self._ERRORS as e:
            raise StoreError(f"알림 큐를 읽을 수 없다: {type(e).__name__}") from e
        return [QueuedAlert(r[0], r[1], r[2] or None, r[3], r[4], r[5], r[6]) for r in rows]

    def done(self, row_id: int) -> None:
        try:
            with self._lock:
                self._db.execute("DELETE FROM alert_queue WHERE id=?", (row_id,))
        except self._ERRORS as e:
            raise StoreError(f"알림 큐를 갱신할 수 없다: {type(e).__name__}") from e

    def failed(self, row_id: int, next_attempt_at: float, error: str) -> None:
        try:
            with self._lock:
                self._db.execute("UPDATE alert_queue SET attempts=attempts+1, next_attempt_at=?, last_error=? WHERE id=?", (next_attempt_at, error[:200], row_id))
        except self._ERRORS as e:
            raise StoreError(f"알림 큐를 갱신할 수 없다: {type(e).__name__}") from e

    def counts(self) -> dict:
        """{pending, oldest_age_s}. /readyz용."""
        try:
            with self._lock:
                n, oldest = self._db.execute("SELECT COUNT(*), MIN(created_at) FROM alert_queue").fetchone()
        except self._ERRORS as e:
            raise StoreError(f"알림 큐를 읽을 수 없다: {type(e).__name__}") from e
        return {"pending": int(n), "oldest_age_s": None if oldest is None else max(0.0, self._clock() - oldest)}


class PgAlertQueue(AlertQueue):
    """PostgreSQL 알림 큐. 연결 클래스와 드라이버 오류는 조립 계층이 주입한다(한계 L2)."""
    KIND = "postgresql"

    def __init__(self, url: str, *, connect, driver_errors: tuple, clock) -> None:
        self._connect, self._ERRORS = connect, tuple(driver_errors)
        super().__init__(url, clock=clock)

    def _open(self, url: str) -> None:
        _migrate("postgresql", url)
        self._db = self._connect(url, "alert_queue")
