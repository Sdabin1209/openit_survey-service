"""Session Store 컴포넌트: 세션 문서와 revision, 턴 멱등 기록을 읽고 쓴다. 구현은 SQLite(기본), PostgreSQL(운영), 메모리(시험) 셋이고 같은 적합성 시험(tests/test_store.py)을 통과한다.

입력 StoreLoadRequest/StoreCommitRequest -> 출력 StoreLoadResult/StoreCommitResult(schemas/). 저장 효과(저장 값·진행 갱신 적용)는 판단 없는 순수 함수(아래 effects 절)이고
어떤 값을 저장할지는 write_plan이 정한다. 한 SQLite 파일 WAL, 연결 하나를 락으로 보호한다(턴은 세션 단위로 직렬화되고 쓰기가 짧다). 여러 프로세스가 같은 파일을 쓰더라도
낙관적 revision 검사가 쓰기 충돌을 막는다.
왜 한 파일인가: SQLite와 PostgreSQL 구현이 같은 SQL을 공유(상속)하고 메모리 구현이 같은 효과 함수를 쓴다. 컴포넌트끼리 import 금지(DESIGN 2절)라 같은 컴포넌트 안에 둔다.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol

from ..wiring.errors import StoreEffectError, StoreError
from ..migrate import migrate
from ..wiring.pg import DRIVER_ERRORS, PgConn

# ---------------------------------------------------------------- 저장 효과(순수 함수, 입력 문서는 바꾸지 않는다)

def slot_key(slot: Mapping) -> tuple:
    return (slot["question_id"], slot.get("repeat_key", ""), slot["slot_id"])


def ordered_questions(bank: Mapping) -> list[dict]:
    return sorted(bank["questions"], key=lambda q: q["order"])


def initial_answer_records(bank: Mapping) -> list[dict]:
    """모든 슬롯을 '없음'으로. 문항 order 순, 반복 키마다, 슬롯 정의 순."""
    out = []
    for q in ordered_questions(bank):
        for rk in (q.get("repeat_keys") or [""]):
            for s in q["slots"]:
                out.append({"slot": {"question_id": q["question_id"], "repeat_key": rk, "slot_id": s["slot_id"]},
                            "current_value": "NONE", "risk_mark": None, "history": []})
    return out


def initial_state(session_id: str, bank: Mapping, first_question_id: str) -> dict:
    """세션 정보 생성 효과."""
    return {
        "session_info": {"session_id": session_id, "question_bank_version": bank["version"]},
        "answer_records": initial_answer_records(bank),
        "progress_record": {"current_question_id": first_question_id, "open_slots": [], "skipped": [],
                            "session_status": "IN_PROGRESS", "pause_history": []},
        "dialogue_log": [],
        "dispatch_log": [],
    }


def model_key(slot: Mapping) -> str:
    """SlotRef -> 모델 키(`Q7_1[soju].amount`)."""
    rk = slot.get("repeat_key", "")
    return f"{slot['question_id']}[{rk}].{slot['slot_id']}" if rk else f"{slot['question_id']}.{slot['slot_id']}"


def slot_ref(key: str) -> dict:
    """모델 키 -> SlotRef. 종류 괄호가 없으면 repeat_key는 빈 문자열."""
    head, slot_id = key.rsplit(".", 1)
    item, _, rest = head.partition("[")
    return {"question_id": item, "repeat_key": rest[:-1] if rest else "", "slot_id": slot_id}


def answers_by_key(records: list) -> dict:
    """저장된 값(answer)이 있는 칸: 모델 키 -> (값, precision)."""
    out = {}
    for r in records:
        cv = r["current_value"]
        if isinstance(cv, dict) and "answer" in cv:
            out[model_key(r["slot"])] = (cv["answer"], r.get("precision", "unspecified"))
    return out


def apply_answer_updates(records: list, updates: list) -> list:
    recs = copy.deepcopy(records)
    index = {slot_key(r["slot"]): r for r in recs}
    for u in updates:
        rec = index.get(slot_key(u["slot"]))
        if rec is None:
            raise StoreEffectError("저장할 슬롯이 응답 기록에 없다")
        cv = rec["current_value"]
        has = isinstance(cv, dict) and "answer" in cv
        if u["action"] == "FIRST_WRITE":
            if has:
                raise StoreEffectError("이미 값이 있는 칸의 첫 저장")
        elif u["action"] == "NEW_REVISION":
            if not has:
                raise StoreEffectError("값이 없는 칸의 정정")
            entry = {"value": copy.deepcopy(cv["answer"]), "precision": rec.get("precision", "unspecified")}
            rec["history"].append(entry)
        else:
            raise StoreEffectError(f"알 수 없는 저장 동작: {u['action']}")
        rec["current_value"] = {"answer": copy.deepcopy(u["value"])}
        rec["precision"] = u["precision"]
    return recs


def apply_progress_update(progress: Mapping, update: Optional[Mapping]) -> dict:
    prog = copy.deepcopy(progress)
    if update is None:
        return prog
    prog["current_question_id"] = update["current_question_id"]
    prog["open_slots"] = copy.deepcopy(update["open_slots"])
    removed = set(update["skipped_remove"])
    prog["skipped"] = [e for e in prog["skipped"] if e["question_id"] not in removed]
    for add in update["skipped_add"]:
        for e in prog["skipped"]:
            if e["question_id"] == add["question_id"]:
                e["reason"] = add["reason"]
                break
        else:
            prog["skipped"].append(copy.deepcopy(add))
    if update["session_status"] is not None:
        prog["session_status"] = update["session_status"]
    prog["pause_history"] = prog["pause_history"] + copy.deepcopy(update["pause_history_add"])
    return prog


def apply_writes(doc: Mapping, writes: Mapping) -> dict:
    """한 턴의 쓰기(TurnWriteSet) 전체를 문서에 반영한다. 하나라도 반영할 수 없으면 StoreEffectError."""
    new = copy.deepcopy(doc)
    new["answer_records"] = apply_answer_updates(doc["answer_records"], writes["answer_updates"])
    new["progress_record"] = apply_progress_update(doc["progress_record"], writes.get("progress_record_update"))
    return new


# ---------------------------------------------------------------- 인터페이스

@dataclass
class TurnRecord:
    input_hash: str
    committed: bool
    result: Optional[dict]


class SessionStorePort(Protocol):
    def ping(self) -> None:
        """열려 있고 읽을 수 있는지. 안 되면 StoreError."""

    def load(self, session_id: str) -> dict:
        """SessionLoadResult(OK / NOT_FOUND / DB_ERROR). DB 오류는 예외가 아니라 DB_ERROR 값으로."""

    def create(self, session_id: str, release_id: str, state: dict) -> int:
        """새 세션을 만든다. 이미 있거나 쓸 수 없으면 StoreError. 만든 revision을 돌려준다."""

    def commit(self, request: dict, *, turn_id: str, input_hash: str) -> dict:
        """SessionCommitResult. 쓰기 전체를 한 번에 반영하거나 하나도 반영하지 않는다.
        같은 트랜잭션에서 (세션, 턴 ID)를 '반영됨'으로 표시한다."""

    def append_log(self, session_id: str, entry: dict) -> None:
        """대화 로그에 덧붙인다. 실패하면 StoreError."""

    def append_dispatch(self, session_id: str, record: dict) -> None:
        """전달 기록에 덧붙인다. 실패하면 StoreError."""

    def release_id_of(self, session_id: str) -> Optional[str]:
        """세션이 묶인 릴리스 ID."""

    def get_turn(self, session_id: str, turn_id: str) -> Optional[TurnRecord]: ...

    def save_turn_result(self, session_id: str, turn_id: str, input_hash: str, result: dict) -> None:
        """턴 결과를 멱등 기록에 남긴다(세션이 없어도 남길 수 있다)."""

    def list_idle(self, before: float) -> list[str]:
        """마지막 기록이 before(epoch 초)보다 오래된 IN_PROGRESS 세션 ID. 유휴 만료에 쓴다."""

    def list_sessions(self, *, status: Optional[str] = None, updated_after: Optional[float] = None, session_ids: Optional[list] = None,
                      limit: int = 50, after: Optional[tuple] = None) -> list[tuple]:
        """결과 조회용 목록: (session_id, status, updated_at, update_seq) 목록을 update_seq(쓸 때마다 매기는 단조 증가 번호) 오름차순으로 최대 limit개.
        updated_after는 epoch 초(초과만, 시각으로 거르기만 한다), after는 마지막으로 본 update_seq(그보다 큰 것만), session_ids는 후보 제한. 순서에 벽시계를 쓰지 않는다(U26, ADR-0034)."""

    def list_ended_before(self, before: float) -> list[str]:
        """끝난(ENDED_*) 세션 중 마지막 기록이 before(epoch 초)보다 오래된 세션 ID. 보존 기간 정리에 쓴다."""

    def delete_sessions(self, session_ids: list) -> int:
        """세션 문서와 그 턴 기록을 지운다. 지운 세션 수."""

    def open_release_ids(self) -> set[str]:
        """아직 끝나지 않은(진행 중·일시 중지) 세션이 묶인 릴리스 ID 집합."""


# ---------------------------------------------------------------- SQLite

OPEN_STATUSES = ("IN_PROGRESS", "PAUSED_RISK")



def _dump(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


class SqliteSessionStore:
    _ERRORS: tuple = (sqlite3.Error,)
    _FOR_UPDATE = ""        # PostgreSQL 구현은 행 잠금을 건다
    # 쓸 때마다 매기는 단조 증가 번호(결과 목록의 순서·커서, U26, ADR-0034). 벽시계(updated_at)는 되감길 수 있어 순서에 쓰지 않는다. PostgreSQL 구현은 시퀀스
    _NEXT_SEQ = "(SELECT COALESCE(MAX(update_seq), 0) + 1 FROM sessions)"
    supports_trace_outbox = True
    outbox_gate = None      # 서비스가 Outbox를 꽂는다(max_pending·dropped_full). 없으면 상한 없이 쓴다

    def __init__(self, path: str, clock=time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.RLock()
        try:
            self._open(path)
        except self._ERRORS as e:
            raise StoreError(f"세션 DB를 열 수 없다: {type(e).__name__}") from e

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

    def _number_lock(self) -> None:
        """update_seq를 받는 모든 쓰기 트랜잭션이 BEGIN 직후 부른다. SQLite는 BEGIN IMMEDIATE가 쓰기 잠금을 커밋까지 쥐어 번호 순서가 커밋 순서다.
        PostgreSQL 구현은 트랜잭션 수준 advisory lock으로 번호 부여를 직렬화한다(ADR-0034)."""

    def create(self, session_id: str, release_id: str, state: dict) -> int:
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._number_lock()
                self._db.execute(f"INSERT INTO sessions(session_id, revision, release_id, status, doc, updated_at, update_seq) VALUES (?,?,?,?,?,?,{self._NEXT_SEQ})",
                                 (session_id, 1, release_id, state["progress_record"]["session_status"], _dump(state), self._clock()))
                self._db.execute("COMMIT")
            except self._ERRORS as e:
                try:
                    self._db.execute("ROLLBACK")
                except self._ERRORS:
                    pass
                raise StoreError(f"세션을 만들 수 없다: {type(e).__name__}") from e
        return 1

    def commit(self, request: dict, *, turn_id: str, input_hash: str, trace_envelope: Optional[dict] = None) -> dict:
        """trace_envelope가 있으면 같은 트랜잭션에 Trace outbox 예약 행을 쓴다(트랜잭셔널 outbox)."""
        sid = request["session_id"]

        def result(status, outcome, revision=None, detail=None):
            return {"session_id": sid, "status": status, "write_outcome": outcome, "revision": revision, "detail": detail}

        with self._lock:
            stage = "begin"
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._number_lock()
                row = self._db.execute("SELECT revision, doc FROM sessions WHERE session_id=?" + self._FOR_UPDATE, (sid,)).fetchone()
                if row is None:
                    self._db.execute("ROLLBACK")
                    return result("DB_ERROR", "NOT_APPLIED", detail="세션이 없다")
                if row[0] != request["expected_revision"]:
                    self._db.execute("ROLLBACK")
                    return result("REVISION_CONFLICT", "NOT_APPLIED", revision=row[0])
                try:
                    new_doc = apply_writes(json.loads(row[1]), request["writes"])
                except StoreEffectError as e:
                    self._db.execute("ROLLBACK")
                    return result("DB_ERROR", "NOT_APPLIED", detail=f"반영할 수 없는 쓰기: {e}")
                rev = row[0] + 1
                self._db.execute(f"UPDATE sessions SET revision=?, status=?, doc=?, updated_at=?, update_seq={self._NEXT_SEQ} WHERE session_id=?",
                                 (rev, new_doc["progress_record"]["session_status"], _dump(new_doc), self._clock(), sid))
                self._db.execute(
                    "INSERT INTO turns(session_id, turn_id, input_hash, committed) VALUES (?,?,?,1) "
                    "ON CONFLICT(session_id, turn_id) DO UPDATE SET committed=1, input_hash=excluded.input_hash",
                    (sid, turn_id, input_hash))
                if trace_envelope is not None:
                    self.outbox_gate.stage_in_txn(self._db, trace_envelope, self._clock())
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
                self._number_lock()
                row = self._db.execute("SELECT revision, doc FROM sessions WHERE session_id=?" + self._FOR_UPDATE, (session_id,)).fetchone()
                if row is None:
                    self._db.execute("ROLLBACK")
                    raise StoreError("세션이 없다")
                doc = json.loads(row[1])
                doc[key].append(item)
                self._db.execute(f"UPDATE sessions SET revision=?, doc=?, updated_at=?, update_seq={self._NEXT_SEQ} WHERE session_id=?", (row[0] + 1, _dump(doc), self._clock(), session_id))
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

    def finalize_turn(self, session_id: str, turn_id: str, input_hash: str, result: Optional[dict], envelope: Optional[dict]) -> None:
        """턴의 마지막 쓰기: 멱등 결과(있으면)와 완성된 Trace outbox 행(있으면)을 한 트랜잭션으로."""
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if result is not None:
                    self._db.execute(
                        "INSERT INTO turns(session_id, turn_id, input_hash, committed, result_json) VALUES (?,?,?,0,?) "
                        "ON CONFLICT(session_id, turn_id) DO UPDATE SET result_json=excluded.result_json",
                        (session_id, turn_id, input_hash, _dump(result)))
                if envelope is not None:
                    self.outbox_gate.finish_in_txn(self._db, envelope, self._clock())
                self._db.execute("COMMIT")
            except self._ERRORS as e:
                try:
                    self._db.execute("ROLLBACK")
                except self._ERRORS:
                    pass
                raise StoreError(f"턴 마무리를 남길 수 없다: {type(e).__name__}") from e

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
            conds.append("update_seq>?"); args.append(after)
        if session_ids is not None:
            if not session_ids:
                return []
            conds.append(f"session_id IN ({','.join('?' * len(session_ids))})"); args += list(session_ids)
        q = "SELECT session_id, status, updated_at, update_seq FROM sessions" + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY update_seq LIMIT ?"
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


# ---------------------------------------------------------------- PostgreSQL

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


# ---------------------------------------------------------------- 메모리

class MemorySessionStore:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}   # id -> {doc, revision, release_id}
        self._turns: dict[tuple, TurnRecord] = {}

    def ping(self) -> None:
        return None

    def _next_seq(self) -> int:
        self._seq = getattr(self, "_seq", 0) + 1          # 결과 목록의 순서·커서용 단조 증가 번호(U26, ADR-0034). 호출자가 self._lock을 쥐고 있다
        return self._seq

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
            self._sessions[session_id] = {"doc": copy.deepcopy(state), "revision": 1, "release_id": release_id, "updated_at": self._clock(), "update_seq": self._next_seq()}
            return 1

    def commit(self, request: dict, *, turn_id: str, input_hash: str, trace_envelope=None) -> dict:   # trace_envelope: 메모리 구현은 outbox가 없어 무시한다
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
                new_doc = apply_writes(s["doc"], request["writes"])
            except StoreEffectError as e:
                return {"session_id": sid, "status": "DB_ERROR", "write_outcome": "NOT_APPLIED", "revision": None,
                        "detail": f"반영할 수 없는 쓰기: {e}"}
            s["doc"] = new_doc
            s["revision"] += 1
            s["updated_at"] = self._clock()
            s["update_seq"] = self._next_seq()
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
            s["updated_at"] = self._clock()
            s["update_seq"] = self._next_seq()

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

    def list_idle(self, before: float) -> list[str]:
        with self._lock:
            return [k for k, s in sorted(self._sessions.items(), key=lambda kv: kv[1]["updated_at"])
                    if s["doc"]["progress_record"]["session_status"] == "IN_PROGRESS" and s["updated_at"] < before]

    def list_sessions(self, *, status=None, updated_after=None, session_ids=None, limit=50, after=None):
        with self._lock:
            rows = [(k, s["doc"]["progress_record"]["session_status"], s["updated_at"], s["update_seq"]) for k, s in self._sessions.items()]
        rows = [r for r in rows if (not status or r[1] == status) and (updated_after is None or r[2] > updated_after)
                and (session_ids is None or r[0] in set(session_ids)) and (after is None or r[3] > after)]
        return sorted(rows, key=lambda r: r[3])[:limit]

    def list_ended_before(self, before: float) -> list[str]:
        with self._lock:
            return [k for k, s in sorted(self._sessions.items(), key=lambda kv: kv[1]["updated_at"])
                    if s["doc"]["progress_record"]["session_status"].startswith("ENDED") and s["updated_at"] < before]

    def delete_sessions(self, session_ids: list) -> int:
        with self._lock:
            n = 0
            for sid in session_ids:
                n += 1 if self._sessions.pop(sid, None) is not None else 0
                for k in [k for k in self._turns if k[0] == sid]:
                    del self._turns[k]
            return n

    def open_release_ids(self) -> set[str]:
        with self._lock:
            return {s["release_id"] for s in self._sessions.values()
                    if s["doc"]["progress_record"]["session_status"] in OPEN_STATUSES}
