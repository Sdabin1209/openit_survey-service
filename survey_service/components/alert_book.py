"""직원 알림 저장소 컴포넌트(AlertStore, PgAlertStore): 알림·이벤트·조치·푸시 구독·접근 기록·신호의 영속 저장. 입력 AlertWrite -> 출력 AlertRead.
알림 행과 이벤트 행은 한 트랜잭션이다. 알림 생성·전달 규칙은 alert_service, 푸시 전송은 link_push가 맡는다. 원래 설명은 alert_service 문서 참고.
"""
from __future__ import annotations

from ..config import REAL_WALL_CLOCK

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional

from boundary_contracts import BoundaryError
from ..migrate import migrate

log = logging.getLogger("survey_service.alerts")


class StoreError(BoundaryError):
    """저장소를 쓸 수 없다(DB 오류 등). 값(발화 등)은 메시지에 담지 않는다. 오류는 BoundaryError(UNAVAILABLE, details.reason=STORE_ERROR)다:
    컴포넌트가 다른 모듈의 오류 클래스를 import하지 않는다(DESIGN 6절). 같은 뜻의 클래스가 저장소 컴포넌트마다 있고 조립 계층이 묶어 준다."""
    def __init__(self, message: str = "저장소를 쓸 수 없다") -> None:
        super().__init__("UNAVAILABLE", message, {"reason": "STORE_ERROR"})


def _migrate(kind: str, target: str) -> None:
    """마이그레이션 실패(migrate는 컴포넌트가 아니라 BoundaryError를 던진다)를 이 컴포넌트의 StoreError로 바꾼다."""
    try:
        migrate(kind, target)
    except BoundaryError as e:
        raise StoreError(e.message) from None


class StaffCallRejected(BoundaryError):
    """재알림·실패 기록 거절. kind: NO_OPEN_ALERT(409), OUTCOME_MISMATCH(409), INTERVAL(429), RETRY_LIMIT(429)."""
    def __init__(self, kind: str, retry_after_s: int = 0) -> None:
        super().__init__("CONFLICT" if kind in ("NO_OPEN_ALERT", "OUTCOME_MISMATCH") else "UNAVAILABLE", kind,
                         {"reason": "STAFF_CALL_REJECTED", "kind": kind})
        self.kind, self.retry_after_s = kind, retry_after_s


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

    def __init__(self, path: str, clock: Callable[[], float] = REAL_WALL_CLOCK) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        try:
            self._open(path)
        except self._ERRORS as e:
            raise StoreError(f"알림 DB를 열 수 없다: {type(e).__name__}") from e

    def _open(self, path: str) -> None:
        _migrate("sqlite", path)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")

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

    # ---- 직원 호출 재알림·실패 기록(CR-H001)
    def latest_alert(self, session_id: str, open_only: bool) -> Optional[dict]:
        rows = self._read(f"SELECT {ALERT_COLS}, last_retry_at, last_retry_turn_id FROM alerts WHERE session_id=?" + (" AND status='OPEN'" if open_only else "") + " ORDER BY n DESC LIMIT 1",
                          (session_id,))
        if not rows:
            return None
        a = _row_to_alert(rows[0][:12])
        a["_last_retry_at"] = None if rows[0][12] is None else float(rows[0][12])      # 내부 값(응답에 싣지 않는다)
        a["_last_retry_turn_id"] = rows[0][13]
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

    def begin_retry(self, n: int, turn_id: str, count_attempt: bool = True) -> dict:
        """재알림 1단계(한 트랜잭션): 시도 횟수 +1(count_attempt), 최근 재알림 시각·turn_id, 새 SSE 이벤트(같은 alert_id, 새 이벤트 순번:
        Last-Event-ID로 이어 받는 콘솔도 받는다). **열린 알림에만** 적용한다(UPDATE ... AND status='OPEN'의 영향 행이 0이면 NO_OPEN_ALERT):
        직원의 해결과 겹쳐도 해결된 알림에 'alert' 이벤트가 나가지 않는다. 보낸 결과는 finish_retry가 남긴다."""
        now = self._clock()

        def fn():
            inc = "delivery_attempts=CAST(CAST(COALESCE(delivery_attempts,'0') AS INTEGER)+1 AS TEXT), " if count_attempt else ""
            cur = self._db.execute(f"UPDATE alerts SET {inc}last_retry_at=?, last_retry_turn_id=? WHERE n=? AND status='OPEN'", (repr(now), turn_id, n))
            if cur.rowcount == 0:
                raise StaffCallRejected("NO_OPEN_ALERT")
            return self._event("alert", n)
        return self._txn(fn)

    def finish_retry(self, session_id: str, n: int, turn_id: str, status: str, response: str) -> None:
        """재알림 2단계(한 트랜잭션): 최근 전달 결과와 멱등 기록. 1단계 뒤 여기 전에 죽으면 같은 turn_id의 재전송은 다시 전달한다(안전 알림이므로 최소 한 번)."""
        def fn():
            self._db.execute("UPDATE alerts SET last_delivery_status=? WHERE n=?", (status, n))
            self._db.execute("INSERT INTO staff_call_actions(session_id, turn_id, action, detail, response, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                             (session_id, turn_id, "retry", None, response, _iso(self._clock())))
        self._txn(fn)

    def record_report(self, session_id: str, n: int, turn_id: str, outcome: str, role: str, user: Optional[str]) -> tuple:
        """실패 기록: 알림의 manual_notice_*, 멱등 기록, 접근 기록(감사), 새 SSE 이벤트를 한 트랜잭션으로. 돌려주는 값: (갱신된 알림 모양, 이벤트).
        해결된 알림에도 기록한다(사후 감사). 이벤트로 직원 콘솔이 안다."""
        now = _iso(self._clock())

        def fn():
            self._db.execute("UPDATE alerts SET manual_notice_outcome=?, manual_notice_reported_at=? WHERE n=?", (outcome, now, n))
            self._db.execute("INSERT INTO staff_access_log(at, role, session_id, outcome, detail, user_id) VALUES (?,?,?,?,?,?)",
                             (now, role, session_id, "STAFF_CALL_REPORT", outcome, user))
            event = self._event("alert", n)
            alert = event["alert"]
            self._db.execute("INSERT INTO staff_call_actions(session_id, turn_id, action, detail, response, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                             (session_id, turn_id, "report", outcome, json.dumps(alert, ensure_ascii=False), now))
            return alert, event
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


class PgAlertStore(AlertStore):
    """PostgreSQL 알림 저장소. 연결·스키마 방언만 다르고 나머지는 AlertStore와 같다."""
    KIND = "postgresql"

    def __init__(self, url: str, *, connect, driver_errors: tuple, clock: Callable[[], float] = REAL_WALL_CLOCK) -> None:
        # 연결 클래스·드라이버 오류는 조립 계층이 주입한다(session_store를 import하지 못한다. 한계 L2). 주입한 연결의 접속 실패 오류는 그대로 올라간다.
        self._connect, self._ERRORS = connect, tuple(driver_errors)
        super().__init__(url, clock)

    def _open(self, url: str) -> None:
        _migrate("postgresql", url)
        self._db = self._connect(url, "알림 DB")

    def ping(self) -> None:
        """/readyz용. 주 연결과 락을 쓰지 않는 별도 짧은 연결로 확인한다."""
        try:
            self._db.probe("alerts")
        except self._ERRORS as e:
            raise StoreError(f"알림 DB를 읽을 수 없다: {type(e).__name__}") from None

