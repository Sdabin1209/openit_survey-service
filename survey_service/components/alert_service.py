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

from ..config import REAL_MONOTONIC

import contextvars
import json
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Protocol

from boundary_contracts import BoundaryError

log = logging.getLogger("survey_service.alerts")


class DeliveryFailed(BoundaryError):
    """알림은 저장됐지만 닿은 전달 경로가 없다(SSE 연결 없음, 푸시 접수 없음)."""

    def __init__(self, message: str) -> None:
        super().__init__("UNAVAILABLE", message, {"reason": "DELIVERY_FAILED"})


class StaffCallRejected(BoundaryError):
    """재알림·실패 기록 거절. kind: NO_OPEN_ALERT(409), OUTCOME_MISMATCH(409), INTERVAL(429), RETRY_LIMIT(429). 한계 L2: alert_book에도 같은 정의가 있다."""

    def __init__(self, kind: str, retry_after_s: int = 0) -> None:
        super().__init__("UNAVAILABLE" if kind in ("INTERVAL", "RETRY_LIMIT") else "CONFLICT", kind, {"reason": "STAFF_CALL_REJECTED", "kind": kind})
        self.kind, self.retry_after_s = kind, retry_after_s


def _is_store_error(e: BoundaryError) -> bool:
    """저장소 컴포넌트가 던진 StoreError인가(구조적 판정: 컴포넌트끼리 클래스를 import하지 않는다)."""
    return (e.details or {}).get("reason") == "STORE_ERROR"

KIND_MAP = {"RISK": "RISK_STOP", "CALL_STAFF": "CALL_STAFF", "USER_CALLED_STAFF": "USER_CALLED_STAFF"}
CURRENT_TURN: contextvars.ContextVar = contextvars.ContextVar("survey_current_turn", default=None)      # (session_id, turn_id): 알림 생성의 멱등 키


CURRENT_PAUSE_REASON: contextvars.ContextVar = contextvars.ContextVar("survey_current_pause_reason", default=None)    # 이 알림이 낳은 정지의 사유(RISK|REASK_LIMIT|OTHER)

# 재알림 기본값(CR-H001, ADR-0021): 계약 PUBLIC_API G절의 기본 권고와 같다. 둘 다 측정하지 않은 값이다(현장 흐름 자료 없음).
DEFAULT_RETRY_MIN_INTERVAL_S = 30
DEFAULT_RETRY_MAX_PER_SESSION = 10
DEFAULT_REPORT_MAX_PER_SESSION = 20      # 실패 기록 상한: 측정하지 않은 값(ADR-0021). 재알림 상한의 두 배로 잡았다: 보고는 재알림마다 한 번씩 + 여유



def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


EVIDENCE_PREFIX = "signal:"      # risk_screen이 evidence에 붙이는 접두사(한계 L2: 복제)


def rule_id_of(alert: Mapping) -> Optional[str]:
    """StaffAlert에서 `<source>:<code>`를 꺼낸다(evidence의 `signal:<source>:<code>`). 없으면 None(한계 L2: risk_screen과 복제)."""
    for r in alert.get("reasons") or []:
        for ev in r.get("evidence") or []:
            if isinstance(ev, str) and ev.startswith(EVIDENCE_PREFIX):
                return ev[len(EVIDENCE_PREFIX):]
    return None


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


class AlertService:
    def __init__(self, store, hub: Optional[AlertHub] = None, push: Optional[PushSender] = None,
                 push_timeout: float = 3.0, retry_min_interval_s: int = DEFAULT_RETRY_MIN_INTERVAL_S,
                 retry_max_per_session: int = DEFAULT_RETRY_MAX_PER_SESSION, report_max_per_session: int = DEFAULT_REPORT_MAX_PER_SESSION, endpoint_check=None,
                 monotonic: Callable[[], float] = REAL_MONOTONIC) -> None:
        self._now = monotonic      # 시계 주입(ADR-0050)
        self.endpoint_check = endpoint_check
        self.store, self.hub, self.push, self.push_timeout = store, hub or AlertHub(), push, push_timeout
        self.retry_min_interval_s, self.retry_max_per_session = max(0, int(retry_min_interval_s)), max(0, int(retry_max_per_session))
        self.report_max_per_session = max(0, int(report_max_per_session))
        self.stats = {"deduplicated": 0, "created": 0, "delivered": 0, "delivery_failed": 0, "push_accepted": 0, "push_failed": 0, "push_gone": 0,
                      "staff_call_retries": 0, "staff_call_retry_delivered": 0, "staff_call_retry_failed": 0, "staff_call_retry_replayed": 0,
                      "staff_call_retry_rejected_interval": 0, "staff_call_retry_rejected_limit": 0, "staff_call_retry_no_open_alert": 0,
                      "staff_call_reports": 0, "staff_call_reports_not_notified": 0, "staff_call_report_rejected_limit": 0}
        self._slock = threading.Lock()
        self._locks = [threading.Lock() for _ in range(64)]      # 세션별(해시 줄무늬 64개) 잠금. 한 세션의 재알림·보고·해결을 직렬로 한다(ADR-0021). 서로 다른 세션은 거의 막지 않는다

    def _session_lock(self, session_id: str) -> threading.Lock:
        return self._locks[hash(session_id) % len(self._locks)]

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
        except BoundaryError as _e:
            if not _is_store_error(_e):
                raise
            log.warning("전달 시도 기록을 남기지 못했다")
        if live + pushed == 0:
            self._count("delivery_failed")
            err = DeliveryFailed("알림은 저장됐지만 닿은 전달 경로가 없다")
            err.alert = event["alert"]                       # 외부 위험 신호 API가 alert_id를 돌려줄 수 있게
            raise err
        self._count("delivered")
        try:
            self.store.mark_delivered(session_id, turn_id, kind)
        except BoundaryError as _e:
            if not _is_store_error(_e):
                raise
            pass
        return event["alert"]

    def _push(self, event: dict) -> int:
        if self.push is None:
            return 0
        try:
            subs = self.store.subscriptions()
        except BoundaryError as _e:
            if not _is_store_error(_e):
                raise
            return 0                                  # 구독을 읽지 못하면 푸시는 못 보낸 것이다(SSE 연결은 따로 센다)
        if not subs:
            return 0
        a = event["alert"]
        payload = json.dumps({"alert_id": a["alert_id"], "session_id": a["session_id"], "kind": a["kind"]},        # 계약 StaffPushPayload의 키만(additionalProperties false). type은 알림 문구에 필요 없다
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
                except BoundaryError as _e:
                    if not _is_store_error(_e):
                        raise
                    pass
        threads = [threading.Thread(target=one, args=(s,), daemon=True) for s in subs]
        [t.start() for t in threads]
        deadline = self._now() + self.push_timeout
        for t in threads:
            t.join(max(0.0, deadline - self._now()))
        with lock:
            done = list(results)
        for r, key in (("ACCEPTED", "push_accepted"), ("GONE", "push_gone"), ("FAILED", "push_failed")):
            self._count(key, done.count(r))
        return done.count("ACCEPTED")

    # ---- 직원 호출 재알림·실패 기록 (CR-H001, PUBLIC_API G절)
    def retry_staff_call(self, session_id: str, turn_id: str) -> dict:
        """그 세션의 열린 알림을 새로 만들지 않고 같은 alert_id로 다시 전달한다. StaffCallRetryResult를 돌려준다.
        같은 turn_id는 저장된 응답을 그대로(시도 횟수를 늘리지 않고, 간격·상한도 다시 보지 않는다). 거절은 StaffCallRejected, 저장소 오류는 StoreError.
        잠금은 확인·기록·SSE 발행까지만 쥔다(해결과의 순서를 지킨다). 웹 푸시 접수 대기와 결과 기록은 잠금 밖이다."""
        lock = self._session_lock(session_id)
        with lock:
            prev = self.store.get_action(session_id, turn_id, "retry")
            if prev is not None:
                self._count("staff_call_retry_replayed")
                return json.loads(prev[1])
            alert = self.store.latest_alert(session_id, open_only=True)
            if alert is None:                     # 열린 알림이 없다(없거나 RESOLVED): 해결된 알림은 다시 보내지 않는다
                self._count("staff_call_retry_no_open_alert")
                raise StaffCallRejected("NO_OPEN_ALERT")
            n = int(alert["alert_id"][3:])
            resumed = alert["_last_retry_turn_id"] == turn_id        # 같은 turn_id가 1단계까지 갔다가 기록 없이 끊겼다: 간격·상한을 건너뛰고 시도 횟수 없이 다시 전달한다(최소 한 번, 안전 방향)
            if not resumed:
                if self.store.count_actions(session_id, "retry") >= self.retry_max_per_session:
                    self._count("staff_call_retry_rejected_limit")
                    raise StaffCallRejected("RETRY_LIMIT")
                last = alert["_last_retry_at"]
                if last is not None:
                    wait = min(self.retry_min_interval_s, self.retry_min_interval_s - (self.store._clock() - last))      # min: 시계가 거꾸로 가도 간격을 넘는 대기를 요구하지 않는다
                    if wait > 0:
                        self._count("staff_call_retry_rejected_interval")
                        raise StaffCallRejected("INTERVAL", max(1, int(wait + 0.999)))
            event = self.store.begin_retry(n, turn_id, count_attempt=not resumed)      # 열린 알림에만 적용(해결과 겹치면 NO_OPEN_ALERT). StoreError는 그대로(503)
            live = self.hub.publish(event)
        ok = live + self._push(event) > 0
        status = "DELIVERED" if ok else "DELIVERY_FAILED"
        result = {"alert_id": alert["alert_id"], "staff_alert": status, "delivery_attempts": event["alert"].get("delivery_attempts", 1),
                  "retry_after_s": self.retry_min_interval_s}
        self.store.finish_retry(session_id, n, turn_id, status, json.dumps(result))
        self._count("staff_call_retries")
        self._count("staff_call_retry_delivered" if ok else "staff_call_retry_failed")
        return result

    def report_manual_notice(self, session_id: str, turn_id: str, outcome: str, role: str, user: Optional[str] = None) -> dict:
        """키오스크의 '직접 알리셨나요?' 응답을 알림에 남기고 접근 기록(감사)에 남기며 SSE 이벤트를 낸다. 갱신된 StaffAlert를 돌려준다.
        같은 turn_id는 같은 응답, 같은 turn_id에 다른 outcome은 OUTCOME_MISMATCH, 세션당 상한 초과는 RETRY_LIMIT(429), 알림이 하나도 없으면 NO_OPEN_ALERT."""
        with self._session_lock(session_id):
            prev = self.store.get_action(session_id, turn_id, "report")
            if prev is not None:
                if prev[0] != outcome:
                    raise StaffCallRejected("OUTCOME_MISMATCH")
                return json.loads(prev[1])
            alert = self.store.latest_alert(session_id, open_only=False)      # 왜 OPEN만이 아닌가: 직원이 먼저 해결했어도 키오스크의 보고는 그 호출의 사후 기록으로 남긴다(감사 가치). 이벤트로 콘솔이 안다
            if alert is None:
                raise StaffCallRejected("NO_OPEN_ALERT")
            if self.store.count_actions(session_id, "report") >= self.report_max_per_session:
                self._count("staff_call_report_rejected_limit")
                raise StaffCallRejected("RETRY_LIMIT")
            out, event = self.store.record_report(session_id, int(alert["alert_id"][3:]), turn_id, outcome, role, user)
            self.hub.publish(event)
            self._count("staff_call_reports")
            if outcome == "NOT_NOTIFIED":
                self._count("staff_call_reports_not_notified")
            return out

    # ---- 해결
    def resolve(self, session_id: str, decision: Optional[str], staff_id: Optional[str] = None) -> int:
        with self._session_lock(session_id):       # 재알림·보고의 발행과 순서를 지킨다: 'resolved' 뒤에 같은 알림의 OPEN 'alert' 이벤트가 나가지 않게
            events = self.store.resolve(session_id, decision, staff_id)
            for e in events:
                self.hub.publish(e)
        return len(events)

    def endpoint_error(self, endpoint: str):
        """푸시 구독 endpoint의 등록 때 검사(SSRF 문법 검사). 문제 사유 문장 또는 None. 검사 함수는 wiring이 꽂는다(endpoint_check)."""
        return self.endpoint_check(endpoint) if self.endpoint_check else None

    def public_key(self) -> Optional[str]:
        """웹 푸시 공개키(VAPID). 푸시가 꺼져 있으면 None."""
        return getattr(self.push, "public_key", None) if self.push is not None else None

    def status(self) -> dict:
        with self._slock:
            s = dict(self.stats)
        s.update(retry_min_interval_s=self.retry_min_interval_s, retry_max_per_session=self.retry_max_per_session, report_max_per_session=self.report_max_per_session)
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


