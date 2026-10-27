"""SQLite Session Store(기본 구현). 한 파일, WAL, 세션당 문서 하나와 정수 revision.

연결은 하나를 락으로 보호해 쓴다(턴은 세션 단위로 직렬화되고 쓰기가 짧다). 여러 프로세스가 같은 파일을 쓰더라도
낙관적 revision 검사가 쓰기 충돌을 막는다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional

from ..errors import StoreEffectError, StoreError
from . import effects
from .base import TurnRecord

OPEN_STATUSES = ("IN_PROGRESS", "PAUSED_RISK")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  revision   INTEGER NOT NULL,
  release_id TEXT NOT NULL,
  status     TEXT NOT NULL,
  doc        TEXT NOT NULL,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_status ON sessions(status);
CREATE TABLE IF NOT EXISTS turns (
  session_id  TEXT NOT NULL,
  turn_id     TEXT NOT NULL,
  input_hash  TEXT NOT NULL,
  committed   INTEGER NOT NULL DEFAULT 0,
  result_json TEXT,
  PRIMARY KEY (session_id, turn_id)
);
"""


def _dump(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


class SqliteSessionStore:
    _ERRORS: tuple = (sqlite3.Error,)
    _FOR_UPDATE = ""        # PostgreSQL 구현은 행 잠금을 건다

    def __init__(self, path: str, clock=time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.RLock()
        try:
            self._open(path)
        except self._ERRORS as e:
            raise StoreError(f"세션 DB를 열 수 없다: {type(e).__name__}") from e

    def _open(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(sessions)").fetchall()]
        if "updated_at" not in cols:                       # 이전 파일 이행
            self._db.execute("ALTER TABLE sessions ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
        self._db.execute("CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(status, updated_at)")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ping(self) -> None:
        with self._lock:
            try:
                self._db.execute("SELECT 1 FROM sessions LIMIT 1").fetchall()
            except self._ERRORS as e:
                raise StoreError(f"세션 DB를 읽을 수 없다: {type(e).__name__}") from e

    def load(self, session_id: str) -> dict:
        with self._lock:
            try:
                row = self._db.execute("SELECT revision, doc FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            except self._ERRORS:
                return {"session_id": session_id, "status": "DB_ERROR", "revision": None, "state": None}
        if row is None:
            return {"session_id": session_id, "status": "NOT_FOUND", "revision": None, "state": None}
        return {"session_id": session_id, "status": "OK", "revision": row[0], "state": json.loads(row[1])}

    def create(self, session_id: str, release_id: str, state: dict) -> int:
        with self._lock:
            try:
                self._db.execute("INSERT INTO sessions(session_id, revision, release_id, status, doc, updated_at) VALUES (?,?,?,?,?,?)",
                                 (session_id, 1, release_id, state["progress_record"]["session_status"], _dump(state), self._clock()))
            except self._ERRORS as e:
                raise StoreError(f"세션을 만들 수 없다: {type(e).__name__}") from e
        return 1

    def commit(self, request: dict, *, turn_id: str, input_hash: str) -> dict:
        sid = request["session_id"]

        def result(status, outcome, revision=None, detail=None):
            return {"session_id": sid, "status": status, "write_outcome": outcome, "revision": revision, "detail": detail}

        with self._lock:
            stage = "begin"
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT revision, doc FROM sessions WHERE session_id=?" + self._FOR_UPDATE, (sid,)).fetchone()
                if row is None:
                    self._db.execute("ROLLBACK")
                    return result("DB_ERROR", "NOT_APPLIED", detail="세션이 없다")
                if row[0] != request["expected_revision"]:
                    self._db.execute("ROLLBACK")
                    return result("REVISION_CONFLICT", "NOT_APPLIED", revision=row[0])
                try:
                    new_doc = effects.apply_writes(json.loads(row[1]), request["writes"])
                except StoreEffectError as e:
                    self._db.execute("ROLLBACK")
                    return result("DB_ERROR", "NOT_APPLIED", detail=f"반영할 수 없는 쓰기: {e}")
                rev = row[0] + 1
                self._db.execute("UPDATE sessions SET revision=?, status=?, doc=?, updated_at=? WHERE session_id=?",
                                 (rev, new_doc["progress_record"]["session_status"], _dump(new_doc), self._clock(), sid))
                self._db.execute(
                    "INSERT INTO turns(session_id, turn_id, input_hash, committed) VALUES (?,?,?,1) "
                    "ON CONFLICT(session_id, turn_id) DO UPDATE SET committed=1, input_hash=excluded.input_hash",
                    (sid, turn_id, input_hash))
                stage = "commit"
                self._db.execute("COMMIT")
                return result("OK", "APPLIED", revision=rev)
            except self._ERRORS as e:
                try:
                    self._db.execute("ROLLBACK")
                except self._ERRORS:
                    pass
                # COMMIT 문 자체가 실패하면 반영 여부를 알 수 없다
                return result("DB_ERROR", "UNKNOWN" if stage == "commit" else "NOT_APPLIED",
                              detail=f"DB 오류: {type(e).__name__}")

    def _append(self, session_id: str, key: str, item: dict) -> None:
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT revision, doc FROM sessions WHERE session_id=?" + self._FOR_UPDATE, (session_id,)).fetchone()
                if row is None:
                    self._db.execute("ROLLBACK")
                    raise StoreError("세션이 없다")
                doc = json.loads(row[1])
                doc[key].append(item)
                self._db.execute("UPDATE sessions SET revision=?, doc=?, updated_at=? WHERE session_id=?", (row[0] + 1, _dump(doc), self._clock(), session_id))
                self._db.execute("COMMIT")
            except self._ERRORS as e:
                try:
                    self._db.execute("ROLLBACK")
                except self._ERRORS:
                    pass
                raise StoreError(f"기록을 덧붙일 수 없다: {type(e).__name__}") from e

    def append_log(self, session_id: str, entry: dict) -> None:
        self._append(session_id, "dialogue_log", entry)

    def append_dispatch(self, session_id: str, record: dict) -> None:
        self._append(session_id, "dispatch_log", record)

    def release_id_of(self, session_id: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT release_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return row[0] if row else None

    def get_turn(self, session_id: str, turn_id: str) -> Optional[TurnRecord]:
        with self._lock:
            row = self._db.execute("SELECT input_hash, committed, result_json FROM turns WHERE session_id=? AND turn_id=?",
                                   (session_id, turn_id)).fetchone()
        if row is None:
            return None
        return TurnRecord(row[0], bool(row[1]), json.loads(row[2]) if row[2] else None)

    def save_turn_result(self, session_id: str, turn_id: str, input_hash: str, result: dict) -> None:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO turns(session_id, turn_id, input_hash, committed, result_json) VALUES (?,?,?,0,?) "
                    "ON CONFLICT(session_id, turn_id) DO UPDATE SET result_json=excluded.result_json",
                    (session_id, turn_id, input_hash, _dump(result)))
            except self._ERRORS as e:
                raise StoreError(f"턴 결과를 남길 수 없다: {type(e).__name__}") from e

    def list_idle(self, before: float) -> list[str]:
        """마지막 기록이 before보다 오래된 진행 중(IN_PROGRESS) 세션. 멈춘(PAUSED_RISK) 세션은 직원이 정하므로 넣지 않는다."""
        with self._lock:
            rows = self._db.execute("SELECT session_id FROM sessions WHERE status='IN_PROGRESS' AND updated_at<? ORDER BY updated_at",
                                    (before,)).fetchall()
        return [r[0] for r in rows]

    def list_sessions(self, *, status=None, updated_after=None, session_ids=None, limit=50, after=None):
        conds, args = [], []
        if status:
            conds.append("status=?"); args.append(status)
        if updated_after is not None:
            conds.append("updated_at>?"); args.append(updated_after)
        if after is not None:
            conds.append("(updated_at>? OR (updated_at=? AND session_id>?))"); args += [after[0], after[0], after[1]]
        if session_ids is not None:
            if not session_ids:
                return []
            conds.append(f"session_id IN ({','.join('?' * len(session_ids))})"); args += list(session_ids)
        q = "SELECT session_id, status, updated_at FROM sessions" + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY updated_at, session_id LIMIT ?"
        with self._lock:
            try:
                return [tuple(r) for r in self._db.execute(q, args + [limit]).fetchall()]
            except self._ERRORS as e:
                raise StoreError(f"세션 목록을 읽을 수 없다: {type(e).__name__}") from e

    def list_ended_before(self, before: float) -> list[str]:
        with self._lock:
            try:
                return [r[0] for r in self._db.execute("SELECT session_id FROM sessions WHERE status LIKE 'ENDED%' AND updated_at<? ORDER BY updated_at", (before,))]
            except self._ERRORS as e:
                raise StoreError(f"세션 목록을 읽을 수 없다: {type(e).__name__}") from e

    def delete_sessions(self, session_ids: list) -> int:
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                n = 0
                for sid in session_ids:
                    n += self._db.execute("DELETE FROM sessions WHERE session_id=?", (sid,)).rowcount
                    self._db.execute("DELETE FROM turns WHERE session_id=?", (sid,))
                self._db.execute("COMMIT")
                return n
            except self._ERRORS as e:
                try:
                    self._db.execute("ROLLBACK")
                except self._ERRORS:
                    pass
                raise StoreError(f"세션을 지울 수 없다: {type(e).__name__}") from e

    def open_release_ids(self) -> set[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT release_id FROM sessions WHERE status IN (?,?)", OPEN_STATUSES).fetchall()
        return {r[0] for r in rows}
