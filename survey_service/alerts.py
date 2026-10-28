"""직원 알림(PUBLIC_API.md '직원 콘솔', D64): 영속 저장, 실시간(SSE), 웹 푸시, 해결 표시.

- 위험 정지(그 밖의 직원 호출 포함)가 나면 알림을 **영속 저장**한다(SQLite, `SURVEY_ALERT_DB`). 알림 행과 이벤트 행은 한 트랜잭션이다.
  저장 뒤 구독자(SSE 연결, 웹 푸시)에게 보낸다. 세션 저장소와는 다른 파일이므로 세션 쓰기 확정과 한 트랜잭션은 아니다:
  쓰기 확정 뒤 알림 저장 전에 죽으면 알림이 없다. 이 경우 같은 turn_id 재시도는 DELIVERY_FAILED 신호를 돌려준다(orchestrator 복구 경로).
- 전달 판정: 알림이 저장되고 전달 경로가 하나 이상 살아 있을 때(SSE 연결이 있거나 푸시 서비스가 접수) DELIVERED. 아니면 DeliveryFailed를 던져
  Orchestrator가 DELIVERY_FAILED로 기록한다. 저장된 알림은 DELIVERY_FAILED여도 목록에서 조회된다.
- 알림과 푸시 본문에는 세션 ID와 종류만 넣는다(환자 발화·개인 정보 없음).
- 웹 푸시의 VAPID 비밀키는 환경 변수로만 받는다. 없으면 푸시 없이 SSE만 동작한다.
- StaffConsole 접점 Protocol의 구현(AlertStaffConsole)이다. SURVEY_STAFF_CONSOLE로 다른 구현을 꽂을 수 있다.
"""
from __future__ import annotations

import base64
import contextvars
import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional, Protocol

from . import netguard
from .errors import StoreError, SurveyServiceError
from .risk_rules import rule_id_of

log = logging.getLogger("survey_service.alerts")

_NEW_COLUMNS = (("alerts", "rule_id"), ("alerts", "resolved_by"), ("staff_access_log", "detail"), ("staff_access_log", "user_id"), ("session_meta", "device_id"), ("alerts", "turn_id"), ("alerts", "delivered"),
                ("alerts", "pause_reason"), ("alerts", "delivery_attempts"), ("alerts", "last_delivery_status"), ("alerts", "last_retry_at"),
                ("alerts", "manual_notice_outcome"), ("alerts", "manual_notice_reported_at"))
KIND_MAP = {"RISK": "RISK_STOP", "CALL_STAFF": "CALL_STAFF", "USER_CALLED_STAFF": "USER_CALLED_STAFF"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED')),
  resolved_by_decision TEXT,
  resolved_at TEXT,
  turn_id TEXT,
  delivered TEXT,
  pause_reason TEXT,
  delivery_attempts TEXT,
  last_delivery_status TEXT,
  last_retry_at TEXT,
  manual_notice_outcome TEXT,
  manual_notice_reported_at TEXT
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts(status, n);
CREATE INDEX IF NOT EXISTS alerts_session ON alerts(session_id, status);
CREATE TABLE IF NOT EXISTS alert_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  alert_n INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff_call_actions (
  session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (session_id, turn_id, action)
);
CREATE TABLE IF NOT EXISTS session_timeline (
  session_id TEXT NOT NULL,
  turn_index INTEGER NOT NULL,
  turn_id TEXT,
  at TEXT NOT NULL,
  PRIMARY KEY (session_id, turn_index)
);
CREATE TABLE IF NOT EXISTS staff_access_log (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  role TEXT NOT NULL,
  session_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail TEXT,
  user_id TEXT
);
CREATE TABLE IF NOT EXISTS session_meta (
  session_id TEXT PRIMARY KEY,
  subject_ref TEXT,
  created_at TEXT NOT NULL,
  device_id TEXT
);
CREATE INDEX IF NOT EXISTS session_meta_subject ON session_meta(subject_ref);
CREATE TABLE IF NOT EXISTS risk_signals (
  session_id TEXT NOT NULL,
  signal_id TEXT NOT NULL,
  source TEXT NOT NULL,
  code TEXT NOT NULL,
  severity TEXT NOT NULL,
  occurred_at TEXT,
  alert_n INTEGER NOT NULL,
  session_status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (session_id, signal_id)
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint TEXT PRIMARY KEY,
  subscription TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


CURRENT_TURN: contextvars.ContextVar = contextvars.ContextVar("survey_current_turn", default=None)      # (session_id, turn_id): 알림 생성의 멱등 키


CURRENT_PAUSE_REASON: contextvars.ContextVar = contextvars.ContextVar("survey_current_pause_reason", default=None)    # 이 알림이 낳은 정지의 사유(RISK|REASK_LIMIT|OTHER)

# 재알림 기본값(CR-H001, ADR-0021): 계약 PUBLIC_API G절의 기본 권고와 같다. 둘 다 측정하지 않은 값이다(현장 흐름 자료 없음).
DEFAULT_RETRY_MIN_INTERVAL_S = 30
DEFAULT_RETRY_MAX_PER_SESSION = 10


class StaffCallRejected(SurveyServiceError):
    """재알림·실패 기록 거절. kind: NO_OPEN_ALERT(409), OUTCOME_MISMATCH(409), INTERVAL(429), RETRY_LIMIT(429)."""

    def __init__(self, kind: str, retry_after_s: int = 0) -> None:
        super().__init__(kind)
        self.kind, self.retry_after_s = kind, retry_after_s


class DeliveryFailed(SurveyServiceError):
    """알림은 저장됐지만 닿은 전달 경로가 없다(SSE 연결 없음, 푸시 접수 없음)."""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


ALERT_COLS = ("n, session_id, kind, created_at, status, resolved_by_decision, resolved_at, pause_reason, delivery_attempts, "
              "last_delivery_status, manual_notice_outcome, manual_notice_reported_at")


def _row_to_alert(r) -> dict:
    a = {"alert_id": f"al-{r[0]}", "session_id": r[1], "kind": r[2], "created_at": r[3], "status": r[4],
         "resolved_by_decision": r[5], "resolved_at": r[6]}
    # 2.1.0 optional 필드(CR-H001·H002)는 값이 있을 때만 싣는다: 옛 행(값 없음)의 모양을 바꾸지 않는다. delivery_attempts는 TEXT 열이라 정수로 바꾼다
    for key, v in (("pause_reason", r[7]), ("delivery_attempts", None if r[8] is None else int(r[8])), ("last_delivery_status", r[9]),
                   ("manual_notice_outcome", r[10]), ("manual_notice_reported_at", r[11])):
        if v is not None:
            a[key] = v
    return a


class AlertStore:
    """알림·이벤트·푸시 구독·세션 메타·접근 기록의 SQLite 저장소. PostgreSQL 구현은 `PgAlertStore`(같은 SQL, 같은 시험)."""

    _ERRORS: tuple = (sqlite3.Error,)

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        try:
            self._open(path)
        except self._ERRORS as e:
            raise StoreError(f"알림 DB를 열 수 없다: {type(e).__name__}") from e

    def _open(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        for table, col in _NEW_COLUMNS:      # 이전 파일 이행
            if col not in [r[1] for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()]:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ping(self) -> None:
        with self._lock:
            try:
                self._db.execute("SELECT 1 FROM alerts LIMIT 1").fetchall()
            except self._ERRORS as e:
                raise StoreError(f"알림 DB를 읽을 수 없다: {type(e).__name__}") from e

    def _read(self, sql: str, args=()):
        """읽기. DB 오류는 StoreError로(호출자가 503으로 알린다)."""
        with self._lock:
            try:
                return self._db.execute(sql, args).fetchall()
            except self._ERRORS as e:
                raise StoreError(f"알림 DB 읽기 실패: {type(e).__name__}") from e

    def _txn(self, fn):
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    out = fn()
                    self._db.execute("COMMIT")
                    return out
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            except self._ERRORS as e:
                raise StoreError(f"알림 DB 쓰기 실패: {type(e).__name__}") from e

    def _event(self, etype: str, n: int) -> dict:
        r = self._db.execute(f"SELECT {ALERT_COLS} FROM alerts WHERE n=?", (n,)).fetchone()
        alert = _row_to_alert(r)
        eid = self._db.execute("INSERT INTO alert_events(type, alert_n, payload) VALUES (?,?,?) RETURNING id",
                               (etype, n, json.dumps(alert, ensure_ascii=False))).fetchone()[0]
        return {"type": etype, "id": eid, "alert": alert}      # SSE의 id는 이벤트 순번이다(알림·해결 이벤트가 각각 하나씩 증가)

    def find_by_turn(self, session_id: str, turn_id: str, kind: str) -> Optional[tuple]:
        """같은 (세션, 턴, 종류)에서 이미 만든 알림 -> (알림 이벤트, 전달됨 여부). 없으면 None. 종류가 다르면(같은 턴의 CALL_STAFF와 RISK_STOP) 합치지 않는다."""
        rows = self._read("SELECT n, delivered FROM alerts WHERE session_id=? AND turn_id=? AND kind=? ORDER BY n LIMIT 1", (session_id, turn_id, kind))
        if not rows:
            return None
        ev = self._read("SELECT id, payload FROM alert_events WHERE alert_n=? AND type='alert' ORDER BY id LIMIT 1", (rows[0][0],))
        if not ev:
            return None
        return {"type": "alert", "id": ev[0][0], "alert": json.loads(ev[0][1])}, rows[0][1] == "1"

    def mark_delivered(self, session_id: str, turn_id: Optional[str], kind: str) -> None:
        if turn_id is not None:
            self._txn(lambda: self._db.execute("UPDATE alerts SET delivered='1' WHERE session_id=? AND turn_id=? AND kind=?", (session_id, turn_id, kind)))

    def create(self, session_id: str, kind: str, rule_id: Optional[str] = None, turn_id: Optional[str] = None,
               pause_reason: Optional[str] = None) -> dict:
        """알림 행과 이벤트 행을 한 트랜잭션으로 쓴다. 돌려주는 값: 이벤트 {type: alert, id: 이벤트 순번, alert}.
        rule_id(위험 규칙 id, 발화 원문 아님)는 알림 행에만 남고 목록·SSE의 알림 모양은 그대로다(직원 세션 조회가 읽는다)."""
        def fn():
            n = self._db.execute("INSERT INTO alerts(session_id, kind, created_at, rule_id, turn_id, pause_reason) VALUES (?,?,?,?,?,?) RETURNING n",
                                 (session_id, kind, _iso(self._clock()), rule_id, turn_id, pause_reason)).fetchone()[0]
            return self._event("alert", n)
        return self._txn(fn)

    def resolve(self, session_id: str, decision: Optional[str], staff_id: Optional[str] = None) -> list[dict]:
        """그 세션의 열린 알림을 모두 해결한다(멱등: 열린 알림이 없으면 빈 목록). 해결 이벤트 {type: resolved, id, alert} 목록."""
        def fn():
            ns = [r[0] for r in self._db.execute("SELECT n FROM alerts WHERE session_id=? AND status='OPEN' ORDER BY n", (session_id,))]
            events = []
            for n in ns:
                self._db.execute("UPDATE alerts SET status='RESOLVED', resolved_by_decision=?, resolved_at=?, resolved_by=? WHERE n=?",
                                 (decision, _iso(self._clock()), staff_id, n))
                events.append(self._event("resolved", n))
            return events
        return self._txn(fn)

    def list(self, status: Optional[str], limit: int, cursor: Optional[str]) -> tuple[list[dict], Optional[str]]:
        """최신부터. cursor는 마지막으로 본 알림 번호(불투명 문자열)."""
        q, args = f"SELECT {ALERT_COLS} FROM alerts", []
        conds = []
        if status:
            conds.append("status=?"); args.append(status)
        if cursor:
            conds.append("n<?"); args.append(int(cursor))
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY n DESC LIMIT ?"
        args.append(limit + 1)
        rows = self._read(q, args)
        items = [_row_to_alert(r) for r in rows[:limit]]
        return items, (str(rows[limit - 1][0]) if len(rows) > limit else None)

    def events_after(self, last_id: int, limit: int = 1000) -> list[dict]:
        """Last-Event-ID(= 이벤트 순번) 뒤의 이벤트."""
        rows = self._read("SELECT id, type, payload FROM alert_events WHERE id>? ORDER BY id LIMIT ?", (last_id, limit))
        return [{"id": r[0], "type": r[1], "alert": json.loads(r[2])} for r in rows]

    def oldest_event_id(self) -> Optional[int]:
        r = self._read("SELECT MIN(id) FROM alert_events")[0][0]
        return None if r is None else int(r)

    def latest_event_id(self) -> int:
        return self._read("SELECT COALESCE(MAX(id),0) FROM alert_events")[0][0]

    def trim_events(self, keep: int = 10000) -> int:
        """오래된 이벤트를 지운다(Last-Event-ID로 이어 받을 수 있는 범위는 최근 keep개). 알림 행은 지우지 않는다."""
        return self._txn(lambda: self._db.execute("DELETE FROM alert_events WHERE id<=(SELECT COALESCE(MAX(id),0)-? FROM alert_events)", (keep,)).rowcount)

    # ---- 직원 세션 조회(StaffSessionView)용
    def alerts_of_session(self, session_id: str) -> list[dict]:
        """그 세션의 알림(오래된 것부터). rule_id가 없는 알림은 null."""
        rows = self._read("SELECT n, kind, status, created_at, rule_id, resolved_by, pause_reason, delivery_attempts, last_delivery_status, "
                          "manual_notice_outcome, manual_notice_reported_at FROM alerts WHERE session_id=? ORDER BY n", (session_id,))
        out = []
        for r in rows:
            d = {"alert_id": f"al-{r[0]}", "kind": r[1], "status": r[2], "created_at": r[3], "rule_id": r[4], "resolved_by": r[5]}
            for k, v in (("pause_reason", r[6]), ("delivery_attempts", None if r[7] is None else int(r[7])), ("last_delivery_status", r[8]),
                         ("manual_notice_outcome", r[9]), ("manual_notice_reported_at", r[10])):
                if v is not None:
                    d[k] = v
            out.append(d)
        return out

    def latest_pause_reason(self, session_id: str) -> Optional[str]:
        """그 세션에서 가장 최근에 정지 사유가 기록된 알림의 사유(없으면 None). 세션 상태 조회가 쓴다."""
        rows = self._read("SELECT pause_reason FROM alerts WHERE session_id=? AND pause_reason IS NOT NULL ORDER BY n DESC LIMIT 1", (session_id,))
        return rows[0][0] if rows else None

    # ---- 직원 호출 재알림·실패 기록(CR-H001)
    def latest_alert(self, session_id: str, open_only: bool) -> Optional[dict]:
        rows = self._read(f"SELECT {ALERT_COLS}, last_retry_at FROM alerts WHERE session_id=?" + (" AND status='OPEN'" if open_only else "") + " ORDER BY n DESC LIMIT 1",
                          (session_id,))
        if not rows:
            return None
        a = _row_to_alert(rows[0][:12])
        a["_last_retry_at"] = None if rows[0][12] is None else float(rows[0][12])      # 내부 값(응답에 싣지 않는다)
        return a

    def get_action(self, session_id: str, turn_id: str, action: str) -> Optional[tuple]:
        """같은 (세션, turn_id, 동작)에서 이미 처리한 결과 -> (detail, 응답 JSON 문자열). 없으면 None."""
        rows = self._read("SELECT detail, response FROM staff_call_actions WHERE session_id=? AND turn_id=? AND action=?", (session_id, turn_id, action))
        return (rows[0][0], rows[0][1]) if rows else None

    def count_actions(self, session_id: str, action: str) -> int:
        return int(self._read("SELECT COUNT(*) FROM staff_call_actions WHERE session_id=? AND action=?", (session_id, action))[0][0])

    def record_delivery(self, n: int, status: str) -> None:
        """최초 전달 시도의 결과를 남긴다(시도 횟수 +1, 최근 결과)."""
        self._txn(lambda: self._db.execute(
            "UPDATE alerts SET delivery_attempts=CAST(CAST(COALESCE(delivery_attempts,'0') AS INTEGER)+1 AS TEXT), last_delivery_status=? WHERE n=?", (status, n)))

    def begin_retry(self, n: int) -> dict:
        """재알림 1단계(한 트랜잭션): 시도 횟수 +1, 최근 재알림 시각, 새 SSE 이벤트(같은 alert_id, 새 이벤트 순번: Last-Event-ID로 이어 받는 콘솔도 받는다).
        돌려주는 값: 구독자에게 보낼 이벤트. 보낸 결과는 finish_retry가 남긴다."""
        now = self._clock()

        def fn():
            self._db.execute("UPDATE alerts SET delivery_attempts=CAST(CAST(COALESCE(delivery_attempts,'0') AS INTEGER)+1 AS TEXT), last_retry_at=? WHERE n=?", (repr(now), n))
            return self._event("alert", n)
        return self._txn(fn)

    def finish_retry(self, session_id: str, n: int, turn_id: str, status: str, response: str) -> None:
        """재알림 2단계(한 트랜잭션): 최근 전달 결과와 멱등 기록. 1단계 뒤 여기 전에 죽으면 같은 turn_id의 재전송은 다시 전달한다(안전 알림이므로 최소 한 번)."""
        def fn():
            self._db.execute("UPDATE alerts SET last_delivery_status=? WHERE n=?", (status, n))
            self._db.execute("INSERT INTO staff_call_actions(session_id, turn_id, action, detail, response, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                             (session_id, turn_id, "retry", None, response, _iso(self._clock())))
        self._txn(fn)

    def record_report(self, session_id: str, n: int, turn_id: str, outcome: str, role: str, user: Optional[str]) -> dict:
        """실패 기록: 알림의 manual_notice_*, 멱등 기록, 접근 기록(감사)을 한 트랜잭션으로. 돌려주는 값: 갱신된 알림 모양."""
        now = _iso(self._clock())

        def fn():
            self._db.execute("UPDATE alerts SET manual_notice_outcome=?, manual_notice_reported_at=? WHERE n=?", (outcome, now, n))
            self._db.execute("INSERT INTO staff_access_log(at, role, session_id, outcome, detail, user_id) VALUES (?,?,?,?,?,?)",
                             (now, role, session_id, "STAFF_CALL_REPORT", outcome, user))
            alert = _row_to_alert(self._db.execute(f"SELECT {ALERT_COLS} FROM alerts WHERE n=?", (n,)).fetchone())
            self._db.execute("INSERT INTO staff_call_actions(session_id, turn_id, action, detail, response, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                             (session_id, turn_id, "report", outcome, json.dumps(alert, ensure_ascii=False), now))
            return alert
        return self._txn(fn)

    def record(self, session_id: str, turn_index: int, turn_id: Optional[str]) -> None:
        """세션 문서에 없는 시각을 남긴다. turn_index -1은 세션 생성, 0 이상은 대화 로그 항목의 위치. 같은 키는 처음 값을 지킨다(멱등)."""
        self._txn(lambda: self._db.execute("INSERT INTO session_timeline(session_id, turn_index, turn_id, at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                                           (session_id, turn_index, turn_id, _iso(self._clock()))))

    def timeline(self, session_id: str) -> dict[int, dict]:
        rows = self._read("SELECT turn_index, turn_id, at FROM session_timeline WHERE session_id=?", (session_id,))
        return {r[0]: {"turn_id": r[1], "at": r[2]} for r in rows}

    def log_access(self, role: str, session_id: str, outcome: str, detail: Optional[str] = None, user: Optional[str] = None) -> None:
        """환자 데이터 조회 기록: (시각, 토큰의 역할 이름, session_id, 결과, 조회 조건 이름). 토큰 값·발화·환자 참조 값은 넣지 않는다. 실패하면 StoreError."""
        self.log_access_many(role, [session_id], outcome, detail, user)

    def log_access_many(self, role: str, session_ids: list, outcome: str, detail: Optional[str] = None, user: Optional[str] = None) -> None:
        """목록 조회: 돌려주는 세션마다 한 행을 한 트랜잭션으로 남긴다."""
        at = _iso(self._clock())
        def fn():
            for sid in session_ids:
                self._db.execute("INSERT INTO staff_access_log(at, role, session_id, outcome, detail, user_id) VALUES (?,?,?,?,?,?)", (at, role, sid, outcome, detail, user))
        self._txn(fn)

    # ---- 세션 메타(환자 참조), 위험 신호, 보존
    def set_subject(self, session_id: str, subject_ref: Optional[str], device_id: Optional[str] = None) -> None:
        """세션 시작 때 한 번(환자 참조, 세션을 만든 기기). 이미 있으면 처음 값을 지킨다."""
        self._txn(lambda: self._db.execute("INSERT INTO session_meta(session_id, subject_ref, created_at, device_id) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                                           (session_id, subject_ref, _iso(self._clock()), device_id)))

    def device_of(self, session_id: str) -> Optional[str]:
        rows = self._read("SELECT device_id FROM session_meta WHERE session_id=?", (session_id,))
        return rows[0][0] if rows else None

    def subject_of(self, session_id: str) -> Optional[str]:
        rows = self._read("SELECT subject_ref FROM session_meta WHERE session_id=?", (session_id,))
        return rows[0][0] if rows else None

    def subjects_of(self, session_ids: list) -> dict:
        out: dict = {}
        for i in range(0, len(session_ids), 500):
            chunk = session_ids[i:i + 500]
            for sid, ref in self._read(f"SELECT session_id, subject_ref FROM session_meta WHERE session_id IN ({','.join('?' * len(chunk))})", chunk):
                out[sid] = ref
        return out

    def sessions_of_subject(self, subject_ref: str) -> list[str]:
        return [r[0] for r in self._read("SELECT session_id FROM session_meta WHERE subject_ref=? ORDER BY session_id", (subject_ref,))]

    def get_signal(self, session_id: str, signal_id: str) -> Optional[dict]:
        rows = self._read("SELECT alert_n, session_status FROM risk_signals WHERE session_id=? AND signal_id=?", (session_id, signal_id))
        return {"alert_id": f"al-{rows[0][0]}", "session_status": rows[0][1]} if rows else None

    def record_signal(self, session_id: str, signal: Mapping, alert_n: int, session_status: str) -> None:
        self._txn(lambda: self._db.execute(
            "INSERT INTO risk_signals(session_id, signal_id, source, code, severity, occurred_at, alert_n, session_status, created_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (session_id, signal["signal_id"], signal["source"], signal["code"], signal.get("severity", "RISK"), signal.get("occurred_at"), alert_n,
             session_status, _iso(self._clock()))))

    def purge_sessions(self, session_ids: list) -> int:
        """보존 기간이 지난 세션의 환자 연결 정보(환자 참조, 시각)와 신호 기록을 지운다. 접근 기록(staff_access_log)은 지우지 않는다. 알림은 세션 ID만 담아 둔다."""
        def fn():
            n = 0
            for sid in session_ids:
                for t in ("session_meta", "session_timeline", "risk_signals"):
                    n += self._db.execute(f"DELETE FROM {t} WHERE session_id=?", (sid,)).rowcount
            return n
        return self._txn(fn)

    # ---- 보존(D88 U4): 기간이 지난 행을 지운다. 멱등
    def purge_access_log(self, older_than_ts: float) -> int:
        cut = _iso(older_than_ts)
        return self._txn(lambda: self._db.execute("DELETE FROM staff_access_log WHERE at<?", (cut,)).rowcount)

    def purge_resolved_alerts(self, older_than_ts: float) -> int:
        """해결된 지 older_than_ts보다 오래된 알림과 그 이벤트를 지운다. 열린(OPEN) 알림은 지우지 않는다."""
        cut = _iso(older_than_ts)

        def fn():
            ns = [r[0] for r in self._db.execute("SELECT n FROM alerts WHERE status='RESOLVED' AND resolved_at<?", (cut,)).fetchall()]
            for n in ns:
                self._db.execute("DELETE FROM alert_events WHERE alert_n=?", (n,))
                self._db.execute("DELETE FROM alerts WHERE n=?", (n,))
            return len(ns)
        return self._txn(fn)

    def access_log(self, session_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        q, args = "SELECT n, at, role, session_id, outcome, user_id FROM staff_access_log", []
        if session_id:
            q += " WHERE session_id=?"; args.append(session_id)
        rows = self._read(q + " ORDER BY n DESC LIMIT ?", args + [limit])
        return [{"n": r[0], "at": r[1], "role": r[2], "session_id": r[3], "outcome": r[4], "user_id": r[5]} for r in rows]

    # ---- 푸시 구독
    def upsert_subscription(self, sub: Mapping) -> bool:
        """등록 또는 갱신. 새로 만들면 True. 같은 endpoint는 멱등."""
        def fn():
            existed = self._db.execute("SELECT 1 FROM push_subscriptions WHERE endpoint=?", (sub["endpoint"],)).fetchone() is not None
            self._db.execute("INSERT INTO push_subscriptions(endpoint, subscription, updated_at) VALUES (?,?,?) "
                             "ON CONFLICT(endpoint) DO UPDATE SET subscription=excluded.subscription, updated_at=excluded.updated_at",
                             (sub["endpoint"], json.dumps(dict(sub), ensure_ascii=False), _iso(self._clock())))
            return not existed
        return self._txn(fn)

    def remove_subscription(self, endpoint: str) -> bool:
        return self._txn(lambda: self._db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,)).rowcount > 0)

    def subscriptions(self) -> list[dict]:
        return [json.loads(r[0]) for r in self._read("SELECT subscription FROM push_subscriptions ORDER BY updated_at")]

    def count_subscriptions(self) -> int:
        return self._read("SELECT COUNT(*) FROM push_subscriptions")[0][0]


class AlertHub:
    """SSE 연결(구독자)마다 큐를 하나씩 둔다. 연결 수가 전달 경로의 하나다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: dict) -> int:
        """연결된 구독자 수(이벤트를 큐에 넣은 수)를 돌려준다. 가득 찬 큐는 느린 연결이라 끊도록 표시한다."""
        n = 0
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
                n += 1
            except queue.Full:
                q.overflowed = True  # type: ignore[attr-defined]   # SSE 루프가 보고 연결을 끊는다(클라이언트는 Last-Event-ID로 이어 받는다)
        return n


class PushSender(Protocol):
    def send(self, subscription: Mapping, payload: bytes) -> str:
        """푸시 서비스가 접수하면 'ACCEPTED', 구독이 만료됐으면 'GONE', 그 밖의 실패는 'FAILED'."""


class WebPushSender:
    """pywebpush로 보낸다. VAPID 비밀키는 환경 변수에서 받은 값을 생성자에 넘긴다(여기서 환경을 읽지 않는다)."""

    def __init__(self, vapid_private_key: str, subject: str, timeout: float = 3.0, ttl: int = 3600, allow_private_hosts: bool = False) -> None:
        from py_vapid import Vapid
        try:                                                      # 키 형식을 시작할 때 확인한다(값은 어디에도 남기지 않는다)
            vapid = (Vapid.from_pem(vapid_private_key.encode()) if "BEGIN" in vapid_private_key
                     else Vapid.from_string(vapid_private_key))   # PEM 또는 base64url DER(웹 푸시 표준 표기)
        except Exception:
            raise ValueError("VAPID 비밀키 형식이 잘못됐다") from None
        if not subject.startswith(("mailto:", "https://")):
            raise ValueError("VAPID subject는 mailto: 또는 https:// 이어야 한다")
        self._vapid, self._subject, self._timeout, self._ttl = vapid, subject, timeout, ttl
        self._allow_private = allow_private_hosts      # 개발·시험 전용. 운영 기본은 공개 주소만(netguard)
        from cryptography.hazmat.primitives import serialization
        raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()      # applicationServerKey(공개키, 비밀 아님)

    def send(self, subscription: Mapping, payload: bytes) -> str:
        from pywebpush import WebPushException, webpush
        if not self._allow_private and not netguard.host_is_public(str(subscription.get("endpoint", ""))):
            log.warning("웹 푸시 대상이 공개 주소가 아니어서 보내지 않는다")          # SSRF 방어(URL은 남기지 않는다)
            return "FAILED"
        try:
            import requests
            session = requests.Session()
            session.max_redirects = 0                              # 리다이렉트를 따르지 않는다(다른 내부 주소로 돌려지는 것을 막는다)
            webpush(subscription_info=dict(subscription), data=payload.decode("utf-8"), vapid_private_key=self._vapid,
                    vapid_claims={"sub": self._subject}, timeout=self._timeout, ttl=self._ttl,
                    headers={"Urgency": "high"}, requests_session=session)
            return "ACCEPTED"
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                return "GONE"
            log.warning("웹 푸시 거부 status=%s", status)
            return "FAILED"
        except Exception as e:                                   # 네트워크 오류, 잘못된 구독 키 등. 내용은 남기지 않는다
            log.warning("웹 푸시 실패 error=%s", type(e).__name__)
            return "FAILED"


class AlertService:
    def __init__(self, store: AlertStore, hub: Optional[AlertHub] = None, push: Optional[PushSender] = None,
                 push_timeout: float = 3.0, retry_min_interval_s: int = DEFAULT_RETRY_MIN_INTERVAL_S,
                 retry_max_per_session: int = DEFAULT_RETRY_MAX_PER_SESSION) -> None:
        self.store, self.hub, self.push, self.push_timeout = store, hub or AlertHub(), push, push_timeout
        self.retry_min_interval_s, self.retry_max_per_session = max(0, int(retry_min_interval_s)), max(0, int(retry_max_per_session))
        self.stats = {"deduplicated": 0, "created": 0, "delivered": 0, "delivery_failed": 0, "push_accepted": 0, "push_failed": 0, "push_gone": 0,
                      "staff_call_retries": 0, "staff_call_retry_delivered": 0, "staff_call_retry_failed": 0, "staff_call_retry_replayed": 0,
                      "staff_call_retry_rejected_interval": 0, "staff_call_retry_rejected_limit": 0, "staff_call_retry_no_open_alert": 0,
                      "staff_call_reports": 0, "staff_call_reports_not_notified": 0}
        self._slock = threading.Lock()
        self._retry_lock = threading.Lock()     # 왜: 재알림의 "간격·상한 확인 -> 전달 -> 기록"을 직렬로 한다. 같은 세션의 동시 요청이 간격 검사를 함께 통과하지 못하게(이 서비스는 한 프로세스다, ADR-0021)

    def _count(self, k: str, n: int = 1) -> None:
        with self._slock:
            self.stats[k] += n

    # ---- 만들기와 전달
    def raise_alert(self, session_id: str, staff_alert_kind: str, rule_id: Optional[str] = None, pause_reason: Optional[str] = None) -> dict:
        """알림을 저장하고 구독자에게 보낸다. 닿은 경로가 없으면 DeliveryFailed(알림은 저장된 채)."""
        kind = KIND_MAP.get(staff_alert_kind, staff_alert_kind)
        pause_reason = pause_reason or CURRENT_PAUSE_REASON.get()
        cur = CURRENT_TURN.get()
        turn_id = cur[1] if cur and cur[0] == session_id else None
        event, redo = None, False
        if turn_id is not None:                  # 같은 (세션, 턴, 종류)의 재전송은 기존 알림을 재사용한다(알림이 쌓이지 않는다)
            prev = self.store.find_by_turn(session_id, turn_id, kind)
            if prev is not None:
                event, was_delivered = prev
                if was_delivered:
                    self._count("deduplicated")
                    return event["alert"]
                redo = True                      # 이전 시도가 전달에 실패했다: 알림은 만들지 않고 전달만 다시 시도한다(실패면 DELIVERY_FAILED를 다시 낸다)
        if event is None:
            event = self.store.create(session_id, kind, rule_id, turn_id, pause_reason)         # StoreError는 그대로 올라간다(저장 실패 = 전달 실패)
            self._count("created")
        live = self.hub.publish(event)
        pushed = self._push(event)
        try:
            self.store.record_delivery(int(event["alert"]["alert_id"][3:]), "DELIVERED" if live + pushed else "DELIVERY_FAILED")      # 시도 횟수와 결과(직원 콘솔이 전달 실패 알림을 구분한다)
        except StoreError:
            log.warning("전달 시도 기록을 남기지 못했다")
        if live + pushed == 0:
            self._count("delivery_failed")
            err = DeliveryFailed("알림은 저장됐지만 닿은 전달 경로가 없다")
            err.alert = event["alert"]                       # 외부 위험 신호 API가 alert_id를 돌려줄 수 있게
            raise err
        self._count("delivered")
        try:
            self.store.mark_delivered(session_id, turn_id, kind)
        except StoreError:
            pass
        return event["alert"]

    def _push(self, event: dict) -> int:
        if self.push is None:
            return 0
        try:
            subs = self.store.subscriptions()
        except StoreError:
            return 0                                  # 구독을 읽지 못하면 푸시는 못 보낸 것이다(SSE 연결은 따로 센다)
        if not subs:
            return 0
        a = event["alert"]
        payload = json.dumps({"type": event["type"], "alert_id": a["alert_id"], "session_id": a["session_id"], "kind": a["kind"]},
                             ensure_ascii=False).encode()
        results: list[str] = []
        lock = threading.Lock()

        def one(sub):
            r = self.push.send(sub, payload)
            with lock:
                results.append(r)
            if r == "GONE":
                try:
                    self.store.remove_subscription(sub["endpoint"])
                except StoreError:
                    pass
        threads = [threading.Thread(target=one, args=(s,), daemon=True) for s in subs]
        [t.start() for t in threads]
        deadline = time.monotonic() + self.push_timeout
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        with lock:
            done = list(results)
        for r, key in (("ACCEPTED", "push_accepted"), ("GONE", "push_gone"), ("FAILED", "push_failed")):
            self._count(key, done.count(r))
        return done.count("ACCEPTED")

    # ---- 직원 호출 재알림·실패 기록 (CR-H001, PUBLIC_API G절)
    def retry_staff_call(self, session_id: str, turn_id: str) -> dict:
        """그 세션의 열린 알림을 새로 만들지 않고 같은 alert_id로 다시 전달한다. StaffCallRetryResult를 돌려준다.
        같은 turn_id는 저장된 응답을 그대로(시도 횟수를 늘리지 않고, 간격·상한도 다시 보지 않는다). 거절은 StaffCallRejected, 저장소 오류는 StoreError."""
        with self._retry_lock:
            prev = self.store.get_action(session_id, turn_id, "retry")
            if prev is not None:
                self._count("staff_call_retry_replayed")
                return json.loads(prev[1])
            alert = self.store.latest_alert(session_id, open_only=True)
            if alert is None:                     # 열린 알림이 없다(없거나 RESOLVED): 해결된 알림은 다시 보내지 않는다
                self._count("staff_call_retry_no_open_alert")
                raise StaffCallRejected("NO_OPEN_ALERT")
            if self.store.count_actions(session_id, "retry") >= self.retry_max_per_session:
                self._count("staff_call_retry_rejected_limit")
                raise StaffCallRejected("RETRY_LIMIT", self.retry_min_interval_s)
            last = alert["_last_retry_at"]
            if last is not None:
                wait = self.retry_min_interval_s - (self.store._clock() - last)
                if wait > 0:
                    self._count("staff_call_retry_rejected_interval")
                    raise StaffCallRejected("INTERVAL", max(1, int(wait + 0.999)))
            n = int(alert["alert_id"][3:])
            event = self.store.begin_retry(n)          # StoreError는 그대로 올라간다(503): 기록 없이 전달하지 않는다
            ok = self.hub.publish(event) + self._push(event) > 0
            status = "DELIVERED" if ok else "DELIVERY_FAILED"
            attempts = event["alert"].get("delivery_attempts", 1)
            result = {"alert_id": alert["alert_id"], "staff_alert": status, "delivery_attempts": attempts, "retry_after_s": self.retry_min_interval_s}
            self.store.finish_retry(session_id, n, turn_id, status, json.dumps(result))
            self._count("staff_call_retries")
            self._count("staff_call_retry_delivered" if ok else "staff_call_retry_failed")
            return result

    def report_manual_notice(self, session_id: str, turn_id: str, outcome: str, role: str, user: Optional[str] = None) -> dict:
        """키오스크의 '직접 알리셨나요?' 응답을 알림에 남기고 접근 기록(감사)에 남긴다. 갱신된 StaffAlert를 돌려준다. 같은 turn_id는 같은 응답,
        같은 turn_id에 다른 outcome은 StaffCallRejected(OUTCOME_MISMATCH). 알림이 하나도 없으면 NO_OPEN_ALERT."""
        with self._retry_lock:
            prev = self.store.get_action(session_id, turn_id, "report")
            if prev is not None:
                if prev[0] != outcome:
                    raise StaffCallRejected("OUTCOME_MISMATCH")
                return json.loads(prev[1])
            alert = self.store.latest_alert(session_id, open_only=False)      # 왜 OPEN만이 아닌가: 직원이 먼저 해결했어도 키오스크의 보고는 그 호출 기록에 남는다
            if alert is None:
                raise StaffCallRejected("NO_OPEN_ALERT")
            out = self.store.record_report(session_id, int(alert["alert_id"][3:]), turn_id, outcome, role, user)
            self._count("staff_call_reports")
            if outcome == "NOT_NOTIFIED":
                self._count("staff_call_reports_not_notified")
            return out

    # ---- 해결
    def resolve(self, session_id: str, decision: Optional[str], staff_id: Optional[str] = None) -> int:
        events = self.store.resolve(session_id, decision, staff_id)
        for e in events:
            self.hub.publish(e)
        return len(events)

    def public_key(self) -> Optional[str]:
        """웹 푸시 공개키(VAPID). 푸시가 꺼져 있으면 None."""
        return getattr(self.push, "public_key", None) if self.push is not None else None

    def status(self) -> dict:
        with self._slock:
            s = dict(self.stats)
        s.update(retry_min_interval_s=self.retry_min_interval_s, retry_max_per_session=self.retry_max_per_session)
        s.update(push_enabled=self.push is not None, sse_connections=self.hub.count(), subscriptions=self.store.count_subscriptions())
        return s


class AlertStaffConsole:
    """StaffConsole 접점 Protocol의 구현: 직원 알림을 AlertService로 보낸다.

    Orchestrator는 직원 알림을 낼 때 deliver를 부르고(staff_alert가 있다), 실패 신호·개입 결과를 알릴 때 한 번 더 부른다(staff_alert 없음).
    알림은 앞의 것만 만든다. 개입 결과는 API 계층이 해결 표시를 한다(결정 값을 알고 있다).
    """

    def __init__(self, service: AlertService) -> None:
        self.service = service

    def deliver(self, console_input: Mapping) -> None:
        sa = console_input.get("staff_alert")
        if sa is None:
            return
        self.service.raise_alert(sa["session_id"], sa["staff_alert"]["kind"], rule_id_of(sa["staff_alert"]))      # 정지 사유는 CURRENT_PAUSE_REASON(graph._deliver가 정한다)


class PgAlertStore(AlertStore):
    """PostgreSQL 알림 저장소. 연결·스키마 방언만 다르고 나머지는 AlertStore와 같다."""
    KIND = "postgresql"

    def _open(self, url: str) -> None:
        from .pg import PgConn, pg_ddl
        self._db = PgConn(url, "알림 DB")
        self._db.script(pg_ddl(_SCHEMA))
        for table, col in _NEW_COLUMNS:           # 이전 스키마 이행(멱등)
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} TEXT")

    def ping(self) -> None:
        """/readyz용. 주 연결과 락을 쓰지 않는 별도 짧은 연결로 확인한다."""
        try:
            self._db.probe("alerts")
        except self._ERRORS as e:
            raise StoreError(f"알림 DB를 읽을 수 없다: {type(e).__name__}") from None


try:
    from .pg import DRIVER_ERRORS as _PG_ERRORS
    PgAlertStore._ERRORS = _PG_ERRORS
except ImportError:      # pragma: no cover
    pass
