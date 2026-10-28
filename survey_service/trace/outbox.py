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
from ..migrate import migrate

# 왜: 세션 commit과 같은 트랜잭션에 쓰는 행은 그때 Trace가 아직 완성되지 않았다(응답 생성·전달이 commit 뒤다). 그래서 commit 트랜잭션에는
# '예약(staged)' 행을 쓰고(내용은 그때까지의 Trace) 턴이 끝나면 같은 행을 완성본으로 바꾼다. 예약 행은 다음 시각을 이 값으로 두어 전송 대상에서
# 빠진다(`due`는 next_attempt_at<=now). 스키마(CHECK 제약)를 바꾸지 않으려고 새 상태 값 대신 이 표식을 쓴다. 값 1e18은 어떤 epoch 초보다 크다는 것뿐이다.
STAGED_AT = 1e18


def _count_pending(db, limit: int) -> int:
    return db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM outbox WHERE state='PENDING' LIMIT ?)", (limit,)).fetchone()[0]


def _insert(db, envelope: dict, now: float, next_attempt_at: float) -> bool:
    cur = db.execute(
        "INSERT INTO outbox(session_id, turn_id, envelope, next_attempt_at, created_at) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
        (envelope["session_id"], envelope["turn_id"], json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), next_attempt_at, now))
    return cur.rowcount == 1


def stage_in_txn(db, envelope: dict, now: float, gate=None) -> bool:
    """호출자가 연 트랜잭션 안에서 예약 행을 쓴다(트랜잭셔널 outbox: 업무 변경과 같은 트랜잭션). gate는 max_pending·dropped_full을 가진 객체(Outbox).
    가득 차면 쓰지 않고 센다(Trace 때문에 업무 쓰기가 실패하면 안 된다: I6). 이미 있으면 False."""
    if gate is not None and gate.max_pending > 0 and _count_pending(db, gate.max_pending) >= gate.max_pending:
        gate.dropped_full += 1
        return False
    return _insert(db, envelope, now, STAGED_AT)


def finish_in_txn(db, envelope: dict, now: float, gate=None) -> bool:
    """턴이 끝났다: 예약 행을 완성본으로 바꿔 전송 대상으로 만든다. 예약 행이 없으면(commit이 없던 턴, 가득 참으로 건너뜀) 새로 넣는다.
    이미 전송 대상이 된 행(유예가 지나 부분 Trace가 승격됐다)은 건드리지 않는다."""
    cur = db.execute("UPDATE outbox SET envelope=?, next_attempt_at=0 WHERE session_id=? AND turn_id=? AND next_attempt_at>=?",
                     (json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), envelope["session_id"], envelope["turn_id"], STAGED_AT))
    if cur.rowcount == 1:
        return True
    if gate is not None and gate.max_pending > 0 and _count_pending(db, gate.max_pending) >= gate.max_pending:
        gate.dropped_full += 1
        return False
    return _insert(db, envelope, now, 0)


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
        migrate("sqlite", path)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")

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

    def promote_stale_staged(self, created_before: float) -> int:
        """턴이 끝나지 못하고 사라진 예약 행(프로세스 사망)을 전송 대상으로 승격한다. 내용은 사망 전까지의 Trace라서 스키마 검증에 걸리면
        LOCAL_INVALID로 DEAD가 된다(조용히 사라지지 않고 /readyz에 보인다)."""
        with self._lock:
            try:
                return self._db.execute("UPDATE outbox SET next_attempt_at=0 WHERE next_attempt_at>=? AND created_at<?",
                                        (STAGED_AT, created_before)).rowcount
            except self._ERRORS as e:
                raise StoreError(f"outbox를 갱신할 수 없다: {type(e).__name__}") from e

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
            rows = self._db.execute("SELECT state, COUNT(*) FROM outbox WHERE next_attempt_at<? GROUP BY state", (STAGED_AT,)).fetchall()
            staged = self._db.execute("SELECT COUNT(*) FROM outbox WHERE next_attempt_at>=?", (STAGED_AT,)).fetchone()[0]
        out = {"PENDING": 0, "SENT": 0, "DEAD": 0}
        out.update({r[0]: r[1] for r in rows})
        if staged:
            out["STAGED"] = staged       # 진행 중인 턴의 예약 행(정상이면 곧 PENDING이 된다)
        return out

    def oldest_pending_age(self, now: float) -> Optional[float]:
        with self._lock:
            r = self._db.execute("SELECT MIN(created_at) FROM outbox WHERE state='PENDING' AND next_attempt_at<?", (STAGED_AT,)).fetchone()
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
