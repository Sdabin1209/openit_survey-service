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
import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional, Protocol

from .errors import StoreError, SurveyServiceError

log = logging.getLogger("survey_service.alerts")

KIND_MAP = {"RISK": "RISK_STOP", "CALL_STAFF": "CALL_STAFF", "USER_CALLED_STAFF": "USER_CALLED_STAFF"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED')),
  resolved_by_decision TEXT,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts(status, n);
CREATE INDEX IF NOT EXISTS alerts_session ON alerts(session_id, status);
CREATE TABLE IF NOT EXISTS alert_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  alert_n INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint TEXT PRIMARY KEY,
  subscription TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class DeliveryFailed(SurveyServiceError):
    """알림은 저장됐지만 닿은 전달 경로가 없다(SSE 연결 없음, 푸시 접수 없음)."""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_alert(r) -> dict:
    return {"alert_id": f"al-{r[0]}", "session_id": r[1], "kind": r[2], "created_at": r[3], "status": r[4],
            "resolved_by_decision": r[5], "resolved_at": r[6]}


class AlertStore:
    """알림·이벤트·푸시 구독의 SQLite 저장소."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        try:
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
        except sqlite3.Error as e:
            raise StoreError(f"알림 DB를 열 수 없다: {type(e).__name__}") from e

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ping(self) -> None:
        with self._lock:
            try:
                self._db.execute("SELECT 1 FROM alerts LIMIT 1").fetchall()
            except sqlite3.Error as e:
                raise StoreError(f"알림 DB를 읽을 수 없다: {type(e).__name__}") from e

    def _read(self, sql: str, args=()):
        """읽기. DB 오류는 StoreError로(호출자가 503으로 알린다)."""
        with self._lock:
            try:
                return self._db.execute(sql, args).fetchall()
            except sqlite3.Error as e:
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
            except sqlite3.Error as e:
                raise StoreError(f"알림 DB 쓰기 실패: {type(e).__name__}") from e

    def _event(self, etype: str, n: int) -> dict:
        r = self._db.execute("SELECT n, session_id, kind, created_at, status, resolved_by_decision, resolved_at FROM alerts WHERE n=?", (n,)).fetchone()
        alert = _row_to_alert(r)
        cur = self._db.execute("INSERT INTO alert_events(type, alert_n, payload) VALUES (?,?,?)",
                               (etype, n, json.dumps(alert, ensure_ascii=False)))
        return {"type": etype, "id": cur.lastrowid, "alert": alert}      # SSE의 id는 이벤트 순번이다(알림·해결 이벤트가 각각 하나씩 증가)

    def create(self, session_id: str, kind: str) -> dict:
        """알림 행과 이벤트 행을 한 트랜잭션으로 쓴다. 돌려주는 값: 이벤트 {type: alert, id: 이벤트 순번, alert}."""
        def fn():
            cur = self._db.execute("INSERT INTO alerts(session_id, kind, created_at) VALUES (?,?,?)",
                                   (session_id, kind, _iso(self._clock())))
            return self._event("alert", cur.lastrowid)
        return self._txn(fn)

    def resolve(self, session_id: str, decision: Optional[str]) -> list[dict]:
        """그 세션의 열린 알림을 모두 해결한다(멱등: 열린 알림이 없으면 빈 목록). 해결 이벤트 {type: resolved, id, alert} 목록."""
        def fn():
            ns = [r[0] for r in self._db.execute("SELECT n FROM alerts WHERE session_id=? AND status='OPEN' ORDER BY n", (session_id,))]
            events = []
            for n in ns:
                self._db.execute("UPDATE alerts SET status='RESOLVED', resolved_by_decision=?, resolved_at=? WHERE n=?",
                                 (decision, _iso(self._clock()), n))
                events.append(self._event("resolved", n))
            return events
        return self._txn(fn)

    def list(self, status: Optional[str], limit: int, cursor: Optional[str]) -> tuple[list[dict], Optional[str]]:
        """최신부터. cursor는 마지막으로 본 알림 번호(불투명 문자열)."""
        q, args = "SELECT n, session_id, kind, created_at, status, resolved_by_decision, resolved_at FROM alerts", []
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

    def latest_event_id(self) -> int:
        return self._read("SELECT COALESCE(MAX(id),0) FROM alert_events")[0][0]

    def trim_events(self, keep: int = 10000) -> int:
        """오래된 이벤트를 지운다(Last-Event-ID로 이어 받을 수 있는 범위는 최근 keep개). 알림 행은 지우지 않는다."""
        return self._txn(lambda: self._db.execute("DELETE FROM alert_events WHERE id<=(SELECT COALESCE(MAX(id),0)-? FROM alert_events)", (keep,)).rowcount)

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

    def __init__(self, vapid_private_key: str, subject: str, timeout: float = 3.0, ttl: int = 3600) -> None:
        from py_vapid import Vapid
        try:                                                      # 키 형식을 시작할 때 확인한다(값은 어디에도 남기지 않는다)
            vapid = (Vapid.from_pem(vapid_private_key.encode()) if "BEGIN" in vapid_private_key
                     else Vapid.from_string(vapid_private_key))   # PEM 또는 base64url DER(웹 푸시 표준 표기)
        except Exception:
            raise ValueError("VAPID 비밀키 형식이 잘못됐다") from None
        if not subject.startswith(("mailto:", "https://")):
            raise ValueError("VAPID subject는 mailto: 또는 https:// 이어야 한다")
        self._vapid, self._subject, self._timeout, self._ttl = vapid, subject, timeout, ttl
        from cryptography.hazmat.primitives import serialization
        raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()      # applicationServerKey(공개키, 비밀 아님)

    def send(self, subscription: Mapping, payload: bytes) -> str:
        from pywebpush import WebPushException, webpush
        try:
            webpush(subscription_info=dict(subscription), data=payload.decode("utf-8"), vapid_private_key=self._vapid,
                    vapid_claims={"sub": self._subject}, timeout=self._timeout, ttl=self._ttl,
                    headers={"Urgency": "high"})
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
                 push_timeout: float = 3.0) -> None:
        self.store, self.hub, self.push, self.push_timeout = store, hub or AlertHub(), push, push_timeout
        self.stats = {"created": 0, "delivered": 0, "delivery_failed": 0, "push_accepted": 0, "push_failed": 0, "push_gone": 0}
        self._slock = threading.Lock()

    def _count(self, k: str, n: int = 1) -> None:
        with self._slock:
            self.stats[k] += n

    # ---- 만들기와 전달
    def raise_alert(self, session_id: str, staff_alert_kind: str) -> dict:
        """알림을 저장하고 구독자에게 보낸다. 닿은 경로가 없으면 DeliveryFailed(알림은 저장된 채)."""
        kind = KIND_MAP.get(staff_alert_kind, staff_alert_kind)
        event = self.store.create(session_id, kind)         # StoreError는 그대로 올라간다(저장 실패 = 전달 실패)
        self._count("created")
        live = self.hub.publish(event)
        pushed = self._push(event)
        if live + pushed == 0:
            self._count("delivery_failed")
            raise DeliveryFailed("알림은 저장됐지만 닿은 전달 경로가 없다")
        self._count("delivered")
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

    # ---- 해결
    def resolve(self, session_id: str, decision: Optional[str]) -> int:
        events = self.store.resolve(session_id, decision)
        for e in events:
            self.hub.publish(e)
        return len(events)

    def public_key(self) -> Optional[str]:
        """웹 푸시 공개키(VAPID). 푸시가 꺼져 있으면 None."""
        return getattr(self.push, "public_key", None) if self.push is not None else None

    def status(self) -> dict:
        with self._slock:
            s = dict(self.stats)
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
        self.service.raise_alert(sa["session_id"], sa["staff_alert"]["kind"])
