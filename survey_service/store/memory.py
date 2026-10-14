"""메모리 Session Store. SQLite 구현과 같은 적합성 시험(tests/test_store.py)을 통과한다."""
from __future__ import annotations

import copy
import threading
from typing import Optional

from ..errors import StoreEffectError, StoreError
from . import effects
from .base import TurnRecord

OPEN_STATUSES = ("IN_PROGRESS", "PAUSED_RISK")


class MemorySessionStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}   # id -> {doc, revision, release_id}
        self._turns: dict[tuple, TurnRecord] = {}

    def ping(self) -> None:
        return None

    def load(self, session_id: str) -> dict:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return {"session_id": session_id, "status": "NOT_FOUND", "revision": None, "state": None}
            return {"session_id": session_id, "status": "OK", "revision": s["revision"], "state": copy.deepcopy(s["doc"])}

    def create(self, session_id: str, release_id: str, state: dict) -> int:
        with self._lock:
            if session_id in self._sessions:
                raise StoreError("세션이 이미 있다")
            self._sessions[session_id] = {"doc": copy.deepcopy(state), "revision": 1, "release_id": release_id}
            return 1

    def commit(self, request: dict, *, turn_id: str, input_hash: str) -> dict:
        sid = request["session_id"]
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return {"session_id": sid, "status": "DB_ERROR", "write_outcome": "NOT_APPLIED", "revision": None,
                        "detail": "세션이 없다"}
            if request["expected_revision"] != s["revision"]:
                return {"session_id": sid, "status": "REVISION_CONFLICT", "write_outcome": "NOT_APPLIED",
                        "revision": s["revision"], "detail": None}
            try:
                new_doc = effects.apply_writes(s["doc"], request["writes"])
            except StoreEffectError as e:
                return {"session_id": sid, "status": "DB_ERROR", "write_outcome": "NOT_APPLIED", "revision": None,
                        "detail": f"반영할 수 없는 쓰기: {e}"}
            s["doc"] = new_doc
            s["revision"] += 1
            self._turns[(sid, turn_id)] = TurnRecord(input_hash, True, None)
            return {"session_id": sid, "status": "OK", "write_outcome": "APPLIED", "revision": s["revision"],
                    "detail": None}

    def _append(self, session_id: str, key: str, item: dict) -> None:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                raise StoreError("세션이 없다")
            s["doc"][key].append(copy.deepcopy(item))
            s["revision"] += 1

    def append_log(self, session_id: str, entry: dict) -> None:
        self._append(session_id, "dialogue_log", entry)

    def append_dispatch(self, session_id: str, record: dict) -> None:
        self._append(session_id, "dispatch_log", record)

    def release_id_of(self, session_id: str) -> Optional[str]:
        with self._lock:
            s = self._sessions.get(session_id)
            return s["release_id"] if s else None

    def get_turn(self, session_id: str, turn_id: str) -> Optional[TurnRecord]:
        with self._lock:
            t = self._turns.get((session_id, turn_id))
            return copy.deepcopy(t) if t else None

    def save_turn_result(self, session_id: str, turn_id: str, input_hash: str, result: dict) -> None:
        with self._lock:
            prev = self._turns.get((session_id, turn_id))
            self._turns[(session_id, turn_id)] = TurnRecord(input_hash, bool(prev and prev.committed), copy.deepcopy(result))

    def open_release_ids(self) -> set[str]:
        with self._lock:
            return {s["release_id"] for s in self._sessions.values()
                    if s["doc"]["progress_record"]["session_status"] in OPEN_STATUSES}
