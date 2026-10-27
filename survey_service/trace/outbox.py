"""Trace 영속 버퍼(outbox). SQLite 파일. (세션, 턴)이 유일하다.

턴 처리는 enqueue 한 번(로컬 INSERT)만 한다. 전송과 재시도는 exporter가 따로 한다(턴 처리와 분리, I6).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional

from ..errors import StoreError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      TEXT NOT NULL,
  turn_id         TEXT NOT NULL,
  envelope        TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','SENT','DEAD')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  last_error      TEXT,
  created_at      REAL NOT NULL,
  finished_at     REAL,
  UNIQUE (session_id, turn_id)
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_attempt_at, id);
"""


@dataclass
class OutboxRow:
    id: int
    session_id: str
    turn_id: str
    envelope: dict
    attempts: int


class Outbox:
    _ERRORS: tuple = (sqlite3.Error,)

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self.max_pending = 0           # 0이면 상한 없음. 서비스는 SURVEY_OUTBOX_MAX_PENDING을 준다(수집이 오래 멈췄을 때 디스크가 무한히 차지 않게)
        self.dropped_full = 0          # 가득 차서 넣지 못한 건수(프로세스 시작 이후)
        self._since_check = 0
        self._full = False
        try:
            self._open(path)
        except self._ERRORS as e:
            raise StoreError(f"outbox를 열 수 없다: {type(e).__name__}") from e

    def _open(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ping(self) -> None:
        with self._lock:
            try:
                self._db.execute("SELECT 1 FROM outbox LIMIT 1").fetchall()
            except self._ERRORS as e:
                raise StoreError(f"outbox를 읽을 수 없다: {type(e).__name__}") from e

    def enqueue(self, envelope: dict, now: float) -> bool:
        """처음 넣으면 True, 이미 있으면 False(먼저 넣은 것을 유지한다)."""
        if self.max_pending > 0:
            with self._lock:
                self._since_check += 1
                if self._full or self._since_check >= max(1, min(100, self.max_pending // 10)):   # 세는 비용을 줄이려고 상한의 1/10(최대 100건)마다, 가득 찼을 때는 매번 센다
                    self._since_check = 0
                    n = self._db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM outbox WHERE state='PENDING' LIMIT ?)", (self.max_pending,)).fetchone()[0]
                    self._full = n >= self.max_pending
                if self._full:
                    self.dropped_full += 1
                    raise StoreError("outbox가 가득 찼다(보내지 못한 Trace가 상한만큼 쌓여 있다)")
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO outbox(session_id, turn_id, envelope, created_at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                    (envelope["session_id"], envelope["turn_id"], json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), now))
                return cur.rowcount == 1
        except self._ERRORS as e:
            raise StoreError(f"outbox에 넣을 수 없다: {type(e).__name__}") from e

    def due(self, now: float, limit: int = 50) -> list[OutboxRow]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, session_id, turn_id, envelope, attempts FROM outbox WHERE state='PENDING' AND next_attempt_at<=? "
                "ORDER BY id LIMIT ?", (now, limit)).fetchall()
        return [OutboxRow(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]

    def mark_sent(self, row_id: int, now: float) -> None:
        with self._lock:
            self._db.execute("UPDATE outbox SET state='SENT', finished_at=?, attempts=attempts+1, last_error=NULL WHERE id=?", (now, row_id))

    def mark_failed(self, row_id: int, next_attempt_at: float, error: str) -> None:
        with self._lock:
            self._db.execute("UPDATE outbox SET attempts=attempts+1, next_attempt_at=?, last_error=? WHERE id=?",
                             (next_attempt_at, error, row_id))

    def mark_dead(self, row_id: int, now: float, error: str) -> None:
        with self._lock:
            self._db.execute("UPDATE outbox SET state='DEAD', finished_at=?, attempts=attempts+1, last_error=? WHERE id=?",
                             (now, error, row_id))

    def counts(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state").fetchall()
        out = {"PENDING": 0, "SENT": 0, "DEAD": 0}
        out.update({r[0]: r[1] for r in rows})
        return out

    def oldest_pending_age(self, now: float) -> Optional[float]:
        with self._lock:
            r = self._db.execute("SELECT MIN(created_at) FROM outbox WHERE state='PENDING'").fetchone()
        return None if r[0] is None else now - r[0]

    def dead_by_code(self) -> dict:
        """DEAD 건수를 원인 코드(last_error의 `:` 앞)별로. 인증 실패(PERMISSION_DENIED)가 조용히 쌓이지 않게 /readyz에 낸다."""
        with self._lock:
            rows = self._db.execute("SELECT last_error, COUNT(*) FROM outbox WHERE state='DEAD' GROUP BY last_error").fetchall()
        out: dict = {}
        for err, n in rows:
            code = (err or "UNKNOWN").split(":", 1)[0].strip() or "UNKNOWN"
            out[code] = out.get(code, 0) + n
        return out

    def dead_rows(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT session_id, turn_id, last_error FROM outbox WHERE state='DEAD' ORDER BY id LIMIT ?",
                                    (limit,)).fetchall()
        return [{"session_id": r[0], "turn_id": r[1], "error": r[2]} for r in rows]

    def purge_dead(self, older_than: float) -> int:
        """DEAD로 끝난 지 older_than보다 오래된 행을 지운다(D88 U4: DEAD도 무기한 남기지 않는다)."""
        with self._lock:
            return self._db.execute("DELETE FROM outbox WHERE state='DEAD' AND finished_at<?", (older_than,)).rowcount

    def purge_sent(self, older_than: float) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM outbox WHERE state='SENT' AND finished_at<?", (older_than,)).rowcount
