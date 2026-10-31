"""공개 API(I10): 키오스크 프론트 <-> 문진 서비스 (boundary-contracts/PUBLIC_API.md, D55, D58, D64, D66).

키오스크(토큰 역할 kiosk)
  POST /v1/sessions                    세션 시작   본문 {session_id, kind: SESSION_START}
  POST /v1/sessions/{id}/turns         환자 발화   본문 {session_id, kind: UTTERANCE, utterance} + 봉투 turn_id (멱등)
  POST /v1/sessions/{id}/end           세션 종료   본문 {session_id, kind: SESSION_END}
  GET  /v1/sessions/{id}/state         세션 상태   SessionState (위험 정지 중 폴링, 재개하면 resume_text, 정지면 pause_reason)
  POST /v1/sessions/{id}/staff-call/retry   직원 재알림  StaffCallRetryRequest -> StaffCallRetryResult (세션을 만든 기기 토큰만, CR-H001)
  POST /v1/sessions/{id}/staff-call/report  직접 알림 보고  StaffCallReport -> StaffAlert (같은 권한)
직원 콘솔(토큰 역할 staff)
  POST /v1/sessions/{id}/intervention  개입 결정   본문 {session_id, decision: RESUME|END} + 봉투 turn_id (멈춘 세션만)
  GET  /v1/staff/alerts                알림 목록   StaffAlertPage
  GET  /v1/staff/sessions/{id}         세션 기록 조회   StaffSessionView (대화 기록, 저장된 값, 알림. 읽을 때마다 접근 기록)
  GET  /v1/staff/alerts/stream         알림 SSE    (처리 함수는 server.py가 건넨다)
  GET  /v1/staff/push-config           푸시 설정   PushConfig (공개키 또는 null)
  POST/DELETE /v1/staff/push-subscriptions  웹 푸시 구독 등록·해지
`turn_id`는 API 봉투 필드다: 본문에서 떼어 낸 나머지를 문진 스키마(FrontendMessage, InterventionDecision)로 검증한다(D66).
응답은 FrontendInput(턴), 위 스키마(조회). 오류는 BoundaryError 본문과 상태 코드(401·403 PERMISSION_DENIED, 404 NOT_FOUND,
409 CONFLICT, 429·503 UNAVAILABLE + Retry-After). 문서가 정하지 않은 점의 해석은 docs/change-requests/CR-009.

HTTP 규약(토큰 -> 역할 401/403, 본문 상한 413, 엄격한 JSON 해석, 405, 오류 본문)은 영역 통신 기반 openit_link.App이 지킨다(D118, CR-link-001).
이 모듈은 경로 표(routes), 토큰 표(tokens), 기기 헤더 인증(authenticate)과 업무 처리 함수만 낸다. 서버(waitress, SSE, 요청 ID)는 server.py가 맡는다.
"""
from __future__ import annotations

import collections
import hmac
import io
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime
from typing import Callable, Mapping, Optional

from boundary_contracts import BoundaryError
from openit_link import App as LinkApp, HttpError, Request, Response, Route

from .. import types

log = logging.getLogger("survey_service.api")

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
TURN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")      # $가 아니라 \Z: $는 끝의 개행 하나를 허용해 "id\n"이 다른 turn_id로 통과한다
MAX_BODY = 64 * 1024     # 요청 본문 상한. 넘으면 라이브러리가 413을 낸다(Route.max_body). 측정값이 아니라 기존 값(64 KiB)을 그대로 쓴다
DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")      # 계약 PUBLIC_API.md F절(2.3.0)의 X-Openit-Device 형식
SUBJECT_REF = re.compile(r"^[A-Za-z0-9._:/#=+-]{1,128}\Z")
STAFF_ID = re.compile(r"^[A-Za-z0-9._:@/#=+-]{1,128}\Z")
USER_ID = re.compile(r"^[A-Za-z0-9._@:+-]{1,128}\Z")      # X-Openit-User: gateway가 인증한 계정 ID. 형식이 어긋나면 개인 ID 없이 역할만 기록한다
SESSION_STATUSES = ("IN_PROGRESS", "PAUSED_RISK", "ENDED_COMPLETE", "ENDED_STAFF", "ENDED_LEFT", "ENDED_USER_STOP")
START_TURN_ID, END_TURN_ID, EXPIRE_TURN_ID = "start", "end", "idle-expire"   # turn_id가 없는 동작의 고정 ID(CR-009)
STATE_NAME = {"IN_PROGRESS": "ACTIVE", "PAUSED_RISK": "PAUSED_RISK"}

# 경로 표(이 컴포넌트가 내는 공개 API 전부, 스트림 포함): (이름, 메서드, 경로, 역할, 사람 머리글 필요, 요청 계약 이름, 알림 서비스 필요)
# 역할은 kiosk / staff / result-reader / risk-source. 결과·위험 신호 역할은 그 토큰을 설정하지 않으면 경로가 없다(기능 꺼짐).
_SPEC = (
    ("start", "POST", "/v1/sessions", "kiosk", False, None, False),
    ("turn", "POST", "/v1/sessions/{sid}/turns", "kiosk", False, None, False),
    ("end", "POST", "/v1/sessions/{sid}/end", "kiosk", False, None, False),
    ("state", "GET", "/v1/sessions/{sid}/state", "kiosk", False, None, False),
    ("staff_retry", "POST", "/v1/sessions/{sid}/staff-call/retry", "kiosk", False, "StaffCallRetryRequest", True),
    ("staff_report", "POST", "/v1/sessions/{sid}/staff-call/report", "kiosk", False, "StaffCallReport", True),
    ("intervention", "POST", "/v1/sessions/{sid}/intervention", "staff", True, None, False),
    ("alerts", "GET", "/v1/staff/alerts", "staff", False, None, True),
    ("stream", "GET", "/v1/staff/alerts/stream", "staff", False, None, True),
    ("staff_session", "GET", "/v1/staff/sessions/{sid}", "staff", False, None, True),
    ("push_config", "GET", "/v1/staff/push-config", "staff", False, None, True),
    ("push_add", "POST", "/v1/staff/push-subscriptions", "staff", False, None, True),
    ("push_unsub", "POST", "/v1/staff/push-subscriptions/unsubscribe", "staff", False, None, True),
    ("push_del", "DELETE", "/v1/staff/push-subscriptions", "staff", False, None, True),      # 2.x에서만 허용하는 폐기 예정 방식(계약 PUBLIC_API.md, 3.0.0에서 제거)
    ("result", "GET", "/v1/results/{sid}", "result-reader", False, None, True),
    ("results", "GET", "/v1/results", "result-reader", False, None, True),
    ("risk_signal", "POST", "/v1/sessions/{sid}/risk-signals", "risk-source", False, None, True),
)
_SPEC_RX = [(name, method, re.compile("^" + re.sub(r"\\\{(\w+)\\\}", r"([^/]+)", re.escape(pat)) + "$"), role) for name, method, pat, role, *_ in _SPEC]
KIOSK_DEVICE_CHECKED = ("state", "turn", "end", "staff_retry", "staff_report")      # 세션을 만든 기기만 접근하는 경로(ADR-0016)


def _err(code: str, message: str, status: Optional[int] = None, headers: Optional[dict] = None, details: Optional[dict] = None) -> HttpError:
    """던질 오류(`raise _err(...)`). 본문은 BoundaryError 기록, 상태는 코드 표(CONVENTIONS 9절)이거나 status, 머리글은 headers."""
    d = {**(details or {}), **({"http_status": status} if status else {})}
    return HttpError(BoundaryError(code, message, d or None), headers)


def _check(name: str, value) -> None:
    """영역 스키마(schemas/<name>)로 본문을 검증한다. 어기면 400 INVALID_RECORD."""
    try:
        types.validate(name, value)
    except types.SchemaViolation as e:
        raise _err("INVALID_RECORD", str(e)[:300]) from None


def _sid(sid: str) -> None:
    if not SESSION_ID.match(sid):
        raise _err("INVALID_RECORD", "session_id는 영문·숫자·-_ 1~64자여야 한다")


def model_key(slot: Mapping) -> str:
    """SlotRef -> 모델 키(`Q7_1[soju].amount`)(한계 L2: 복제)."""
    rk = slot.get("repeat_key", "")
    return f"{slot['question_id']}[{rk}].{slot['slot_id']}" if rk else f"{slot['question_id']}.{slot['slot_id']}"


class PublicApi:
    """공개 API(I10)의 받는 쪽. 경로 목록(`routes`)을 openit_link.App에 올린다. 처리 함수는 (상태, 본문, 머리글)을 내거나 오류(`_err`)를 던진다."""

    def __init__(self, orchestrator, token: str, *, staff_token: Optional[str] = None, max_inflight: int = 32,
                 max_utterance_chars: int = 1000, alerts=None, max_subscriptions: int = 100,
                 retry_after_seconds: int = 1, push_allow_private: bool = False, state_poll_min_interval: float = 0.5, result_token: Optional[str] = None,
                 risk_token: Optional[str] = None, require_subject_ref: bool = False, device_tokens: Optional[dict] = None) -> None:
        self._now = getattr(orchestrator, "clock", time.monotonic)        # 주입한 단조 시계(ADR-0050): 오케스트레이터와 같은 시계로 턴 마감·상태 조회 간격을 센다
        self.device_tokens = dict(device_tokens or {})        # {device_id: 토큰}. 2.x 호환. 세션을 만든 기기의 토큰으로만 그 세션에 접근한다 (D88 U8, ADR-0016)
        token = token or None                                  # 키오스크 토큰은 2.x 호환이다. 없어도 된다: 기기 인증은 게이트웨이 mTLS가 넘기는 X-Openit-Device다 (ADR-0031)
        if staff_token is not None and (not staff_token or (token is not None and hmac.compare_digest(staff_token.encode(), token.encode()))):
            raise ValueError("직원 토큰은 비어 있지 않고 키오스크 토큰과 달라야 한다")
        toks = [t for t in (token, staff_token, result_token, risk_token, *self.device_tokens.values()) if t is not None]
        if (result_token is not None and not result_token) or (risk_token is not None and not risk_token) or len(set(toks)) != len(toks):
            raise ValueError("토큰은 역할마다 비어 있지 않고 서로 달라야 한다")
        self.result_token, self.risk_token, self.require_subject_ref = result_token, risk_token, require_subject_ref
        self.orch, self.token, self.staff_token = orchestrator, token, staff_token
        self.alerts, self.max_subscriptions = alerts, max_subscriptions
        self.max_chars = max_utterance_chars
        self.retry_after = max(1, int(retry_after_seconds))
        self.push_allow_private = push_allow_private     # 개발·시험 전용(내부 푸시 대역). 운영 기본은 false
        self.state_poll_min_interval = state_poll_min_interval
        self._polls: dict[str, float] = {}
        self._poll_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._tl = threading.local()
        self.stats: dict = collections.defaultdict(int)
        self._stats_lock = threading.Lock()
        self._links: dict = {}

    # ------------------------------------------------------------------ 라이브러리 연결
    def _tokens(self) -> dict:
        t: dict = {}
        for tok, role in ((self.token, "kiosk"), (self.staff_token, "staff"), (self.result_token, "result-reader"), (self.risk_token, "risk-source")):
            if tok is not None:
                t[tok] = role
        for dev, tok in self.device_tokens.items():
            t[tok] = {"role": "kiosk", "actor": dev}      # 기기 토큰: 호출자 ID(Request.actor)가 기기 ID다
        return t

    def _device_auth(self, headers: Mapping[str, str]):
        """토큰이 없을 때만 라이브러리가 부른다. 게이트웨이가 mTLS로 확인한 기기(X-Openit-Device)는 키오스크 경로에서만 힘이 있다 (ADR-0031)."""
        dev = headers.get("x-openit-device")
        if dev is None or getattr(self._tl, "route_role", None) != "kiosk":
            return None
        if not DEVICE_ID.match(dev):
            raise _err("INVALID_RECORD", "X-Openit-Device 형식이 아니다")
        return "kiosk", dev

    def routes(self, stream: Optional[Callable[[Request], Response]] = None) -> list:
        """공개 API 경로 목록. stream은 알림 SSE 처리 함수(서버가 허브·저장소를 알고 있으므로 server.py가 건넨다)."""
        out = []
        for name, method, pattern, role, user, contract, needs_alerts in _SPEC:
            if (needs_alerts and self.alerts is None) or (role == "result-reader" and self.result_token is None) or (role == "risk-source" and self.risk_token is None):
                continue      # 알림 서비스가 없거나 그 역할의 토큰을 설정하지 않았으면 기능이 꺼진 것이다
            if name == "stream":
                handler = stream or self._stream_unwired
            else:
                handler = self._guarded(name)
            out.append(Route(method, pattern, handler, frozenset({role}), request_schema=contract, require_user=user, max_body=MAX_BODY))
        return out

    @staticmethod
    def _stream_unwired(_req: Request) -> Response:
        raise _err("UNAVAILABLE", "알림 스트림은 서버(server.py)가 연결한다")

    def link_app(self, ready: Optional[Callable[[], tuple]] = None, stream: Optional[Callable[[Request], Response]] = None) -> LinkApp:
        """이 API를 담은 WSGI 앱(/healthz, /readyz 포함). 인증·본문 해석·413·오류 본문은 라이브러리가 한다."""
        return LinkApp(self.routes(stream), self._tokens(), ready=ready, authenticate=self._device_auth)

    def _guarded(self, name: str) -> Callable[[Request], Response]:
        def handler(req: Request) -> Response:
            self._tl.device = req.actor                       # 세션을 만든 기기(기기 토큰 또는 기기 헤더)
            self._tl.user = req.user if isinstance(req.user, str) and USER_ID.match(req.user) else None       # 접근 기록의 개인 ID(D88 U7). gateway가 인증해 붙인 헤더만 믿는다
            if not self._slots.acquire(blocking=False):
                raise _err("UNAVAILABLE", "요청이 너무 많다", 429)
            try:
                status, body, headers = self._call(name, req)
            finally:
                self._slots.release()
            return Response(status, body, headers)
        return handler

    def _call(self, name: str, req: Request):
        sid = req.params.get("sid")
        if sid is not None:
            _sid(sid)      # 경로 조각은 라이브러리가 퍼센트 해제한다(%00 등): 저장소를 부르기 전에 형식부터 본다
        if req.role == "kiosk" and name in KIOSK_DEVICE_CHECKED:
            self._device_denied(sid)
        if req.role == "kiosk" and name == "start" and isinstance(req.body, dict):
            s = req.body.get("session_id")
            self._device_denied(s if isinstance(s, str) else None, allow_new=True)
        return getattr(self, "_" + name)(req)      # 이름으로 찾는다(시험이 처리 함수를 바꿔 끼울 수 있다)

    # ------------------------------------------------------------------ 진입 (시험·직접 호출용 어댑터와 서버 진입)
    def dispatch(self, environ: dict, link: LinkApp) -> tuple:
        """/v1/ 요청 하나를 라이브러리 앱으로 처리한다 -> (상태, 머리글, 본문). 요청 단위 카운터·거절 로그·기본 Retry-After를 여기서 붙인다."""
        self._tl.t0 = self._now()                      # 턴 총 마감은 요청을 받은 때부터 센다
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        user = (environ.get("HTTP_X_OPENIT_USER") or "").strip()
        if user:
            environ["HTTP_X_OPENIT_USER"] = user
        else:
            environ.pop("HTTP_X_OPENIT_USER", None)         # 빈 값은 없는 것과 같다(사람 행위자 필요 경로는 401)
        r = self._route(method, path)
        self._tl.route_role = r[2] if r else None
        if "HTTP_X_OPENIT_DEVICE" in environ:
            environ = dict(environ)
            if self._tl.route_role == "kiosk":
                environ.pop("HTTP_AUTHORIZATION", None)      # 게이트웨이가 확인한 기기가 토큰보다 우선한다(ADR-0031). 라이브러리는 토큰이 없을 때만 _device_auth를 부른다
            else:
                environ.pop("HTTP_X_OPENIT_DEVICE")          # 키오스크 경로 밖에서는 기기 헤더에 힘이 없다
        status, headers, data = link.handle(environ)
        if status in (429, 503) and "Retry-After" not in headers and self._reason(data) != "RETRY_LIMIT":      # 상한 초과는 기다려도 풀리지 않는다: 기본 헤더도 붙이지 않는다
            headers = {**headers, "Retry-After": str(self.retry_after)}
        self._record(method, path, status)
        return status, headers, data

    @staticmethod
    def _reason(data) -> Optional[str]:
        if not isinstance(data, (bytes, bytearray)):
            return None
        try:
            return ((json.loads(data) or {}).get("details") or {}).get("reason")
        except ValueError:
            return None

    def handle(self, method: str, path: str, auth: str, raw: bytes, user: Optional[str] = None, device: Optional[str] = None) -> tuple[int, dict, dict]:
        """(상태 코드, 본문, 추가 헤더). 시험·직접 호출용 어댑터: 인자를 WSGI environ으로 만들어 라이브러리 앱에 넘긴다.
        user는 게이트웨이가 인증한 계정 ID(X-Openit-User), device는 게이트웨이가 mTLS로 확인한 기기 ID(X-Openit-Device)."""
        p, _, qs = path.partition("?")
        environ = {"REQUEST_METHOD": method, "PATH_INFO": p, "REQUEST_URI": path, "QUERY_STRING": qs,
                   "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
        for key, val in (("HTTP_AUTHORIZATION", auth), ("HTTP_X_OPENIT_USER", user), ("HTTP_X_OPENIT_DEVICE", device)):
            if val:
                environ[key] = val
        if "default" not in self._links:
            self._links["default"] = self.link_app()
        status, headers, data = self.dispatch(environ, self._links["default"])
        body = json.loads(data) if isinstance(data, (bytes, bytearray)) and data and "json" in headers.get("Content-Type", "") else (None if data in (b"", None) else data)
        return status, body, {k: v for k, v in headers.items() if k not in ("Content-Type", "Cache-Control", "Content-Length")}

    def _record(self, method: str, path: str, status: int) -> None:
        """핵심 카운터(/readyz의 api)와 거절 경고 로그(E1): 401·403·429는 경로 이름(식별자 없는 틀)과 상태만 남긴다. 토큰·본문·세션 ID는 남기지 않는다."""
        with self._stats_lock:
            self.stats["requests"] += 1
            self.stats[f"status_{status // 100}xx"] += 1
            if status in (401, 403):
                self.stats["auth_failures"] += 1
            elif status == 429:
                self.stats["rate_limited"] += 1
        if status in (401, 403, 429):
            r = self._route(method, path.partition("?")[0])
            log.warning("요청 거절 status=%s method=%s route=%s", status, method, r[0] if r else "unknown")

    def api_stats(self) -> dict:
        with self._stats_lock:
            return dict(self.stats)

    @staticmethod
    def _route(method: str, path: str):
        """(경로 이름, 경로의 세션 ID | None, 역할) 또는 None. 로그의 경로 이름과 시험이 쓴다."""
        for name, m, rx, role in _SPEC_RX:
            if m == method:
                hit = rx.match(path)
                if hit:
                    return name, (hit.group(1) if hit.groups() else None), role
        return None

    def _device_denied(self, sid: Optional[str], allow_new: bool = False) -> None:
        """세션을 만든 기기(헤더의 기기 ID, 또는 2.x 호환 기기 토큰)로만 접근한다. 다른 기기(또는 기기 없는 공유 토큰)이면 세션이 없는 것처럼 404.
        기기를 쓰지 않는 구성(공유 토큰만)은 확인하지 않는다(하위 호환)."""
        dev = getattr(self._tl, "device", None)
        if self.alerts is None or not sid or (dev is None and not self.device_tokens):
            return
        try:
            owner = self.alerts.store.device_of(sid)
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "지금은 처리할 수 없다") from None
        if owner != dev and (owner is not None or dev is not None) and not (allow_new and owner is None):       # 기기 토큰은 기록된 소유가 없는 세션(공유 토큰이 만든 것, 메타 없음)에도 접근하지 못한다
            log.warning("다른 기기의 세션 접근을 거부했다 route_session=%s", "*")
            raise _err("NOT_FOUND", "모르는 세션이다")

    # ------------------------------------------------------------------ 검증
    @staticmethod
    def _envelope(body: Mapping):
        """봉투 필드 turn_id를 떼어 낸다. (나머지 본문, turn_id | None). 형식은 본문 스키마가 이미 봤다."""
        rest = dict(body)
        return rest, rest.pop("turn_id", None)

    @staticmethod
    def _same_session(rest: Mapping, path_sid: str) -> None:
        if rest["session_id"] != path_sid:
            raise _err("INVALID_RECORD", "경로의 세션 ID와 본문의 session_id가 다르다")

    # ------------------------------------------------------------------ 동작
    def _state_of(self, sid: str):
        r = self.orch.store.load(sid)
        if r["status"] == "DB_ERROR":
            return "ERROR", None
        return (r["state"]["progress_record"]["session_status"] if r["status"] == "OK" else None), r

    def _run(self, sid: str, turn_id: str, msg: Mapping):
        try:
            out = self.orch.handle_turn(turn_id=turn_id, frontend_message=msg, started=getattr(self._tl, 't0', None))
        except self.orch.errors.TurnIdConflict:
            raise _err("CONFLICT", "같은 turn_id에 다른 입력이 왔다") from None
        except self.orch.errors.TurnInProgress:
            raise self._in_progress() from None
        return 200, out.frontend_input or {"response_text": None, "turn_failure_signal": None}, {}

    def _in_progress(self) -> HttpError:
        """같은 turn_id가 처리 중. `code: CONFLICT`(409), `details.reason: TURN_IN_PROGRESS`, `Retry-After` 헤더(HTTP.md의 409 사유 규약, CR-015 처리됨).
        입력 불일치 409(details 없음)와 구분된다."""
        return _err("CONFLICT", "같은 turn_id가 처리 중이다. Retry-After 뒤 같은 turn_id로 다시 보낸다", 409,
                    {"Retry-After": str(self.retry_after)}, details={"reason": "TURN_IN_PROGRESS"})

    def _replayed(self, sid: str, turn_id: str) -> bool:
        try:
            return self.orch.store.get_turn(sid, turn_id) is not None
        except Exception:
            return False

    def _start(self, req: Request):
        body = req.body
        _check("SessionStartBody", body)
        rest, tid = self._envelope(body)
        subject = rest.pop("subject_ref", None)             # 환자 참조는 문진 메시지(FrontendMessage)가 아니라 세션 메타다
        issued = False
        if "session_id" not in rest:
            # 왜: 세션 ID는 서버가 CSPRNG로 발급한다(계약 PUBLIC_API.md 2.2.0, D106; OWASP 세션 관리: 예측할 수 없는 값). token_urlsafe(32) = 256비트 -> 43자(영문·숫자·-_)로 1~64자 안
            rest["session_id"] = secrets.token_urlsafe(32)
            issued = True
        else:
            log.warning("폐기 예정 방식: 프론트가 session_id를 정해 보냈다(서버 발급으로 바꿀 것, 3.0.0에서 제거)")      # 값은 로그에 남기지 않는다
        sid, tid = rest["session_id"], tid or START_TURN_ID
        if not self._replayed(sid, tid):
            status, _ = self._state_of(sid)
            if status == "ERROR" or (status is None and self.orch.releases.active() is None):
                raise _err("UNAVAILABLE", "지금은 세션을 시작할 수 없다")
            if status and status.startswith("ENDED"):
                raise _err("CONFLICT", "이미 끝난 세션이다. 새 세션 ID로 시작해야 한다")
            if status is None and subject is None and self.require_subject_ref:
                raise _err("INVALID_RECORD", "subject_ref가 필요하다")
            if status is None and self.alerts is not None:
                try:
                    self.alerts.store.set_subject(sid, subject, getattr(self._tl, 'device', None))       # 세션을 만들기 전에 남긴다(환자 참조, 만든 기기): 못 남기면 시작하지 않는다
                except self.orch.errors.StoreError:
                    raise _err("UNAVAILABLE", "세션 메타를 저장할 수 없다") from None
        code, out, hdr = self._run(sid, tid, rest)
        if issued and code == 200 and isinstance(out, dict):
            out = {**out, "session_id": sid}        # 서버가 발급한 ID를 돌려준다. 프론트가 정한 ID는 되돌리지 않는다(2.x 응답 모양 유지)
        return code, out, hdr

    def _turn(self, req: Request):
        _check("UtteranceBody", req.body)
        rest, tid = self._envelope(req.body)
        self._same_session(rest, req.params["sid"])
        if len(rest["utterance"]) > self.max_chars:      # 상한은 설정(SURVEY_API_MAX_UTTERANCE_CHARS)이라 스키마에 못 쓴다
            raise _err("INVALID_RECORD", f"utterance가 {self.max_chars}자를 넘는다")
        sid = rest["session_id"]
        if not self._replayed(sid, tid):
            status, _ = self._state_of(sid)
            if status == "ERROR":
                raise _err("UNAVAILABLE", "지금은 처리할 수 없다")
            if status is None:
                raise _err("NOT_FOUND", "모르는 세션이다. 먼저 세션을 시작해야 한다")
            if status.startswith("ENDED"):
                raise _err("CONFLICT", "이미 끝난 세션이다")
        return self._run(sid, tid, rest)

    def _end(self, req: Request):
        _check("SessionEndBody", req.body)
        rest, tid = self._envelope(req.body)
        self._same_session(rest, req.params["sid"])
        sid, tid = rest["session_id"], tid or END_TURN_ID
        if not self._replayed(sid, tid):
            status, _ = self._state_of(sid)
            if status == "ERROR":
                raise _err("UNAVAILABLE", "지금은 처리할 수 없다")
            if status is None:
                raise _err("NOT_FOUND", "모르는 세션이다")
            if status.startswith("ENDED"):                 # 이미 끝난 세션의 종료는 같은 결과(말할 것 없음)
                return 200, {"response_text": None, "turn_failure_signal": None}, {}
        res = self._run(sid, tid, rest)
        if res[0] == 200:
            self._resolve_ended(sid)
        return res

    def _intervention(self, req: Request):
        user = req.user      # 없으면 Route.require_user가 401로 걸렀다
        if not STAFF_ID.match(user):
            raise _err("INVALID_RECORD", "X-Openit-User 형식이 아니다")
        _check("InterventionBody", req.body)
        rest, tid = self._envelope(req.body)
        rest.pop("staff_id", None)        # 본문의 staff_id는 믿지 않는다(헤더만)
        self._same_session(rest, req.params["sid"])
        sid = rest["session_id"]
        if not self._replayed(sid, tid):
            status, _ = self._state_of(sid)
            if status == "ERROR":
                raise _err("UNAVAILABLE", "지금은 처리할 수 없다")
            if status is None:
                raise _err("NOT_FOUND", "모르는 세션이다")
            if status != "PAUSED_RISK":
                raise _err("CONFLICT", "멈춘 세션이 아니다")
        try:
            out = self.orch.handle_turn(turn_id=tid, intervention_decision=dict(rest), staff_id=user, started=getattr(self._tl, 't0', None))
        except self.orch.errors.TurnIdConflict:
            raise _err("CONFLICT", "같은 turn_id에 다른 입력이 왔다") from None
        except self.orch.errors.TurnInProgress:
            raise self._in_progress() from None
        self._resolve_if_applied(out, sid, rest["decision"], user)
        return 200, out.frontend_input or {"response_text": None, "turn_failure_signal": None}, {}

    # ------------------------------------------------------------------ 세션 상태 (키오스크 폴링)
    def _state(self, req: Request):
        sid = req.params["sid"]
        _sid(sid)
        if self.state_poll_min_interval > 0:                  # 폴링 빈도 상한(세션마다)
            now = self._now()
            with self._poll_lock:
                last = self._polls.get(sid)
                if last is not None and now - last < self.state_poll_min_interval:
                    raise _err("UNAVAILABLE", "상태 조회가 너무 잦다", 429)
                self._polls[sid] = now
                if len(self._polls) > 2000:
                    self._polls = {k: v for k, v in self._polls.items() if now - v < 60}
        status, r = self._state_of(sid)
        if status == "ERROR":
            raise _err("UNAVAILABLE", "지금은 조회할 수 없다")
        if status is None:
            raise _err("NOT_FOUND", "모르는 세션이다")
        state = "ENDED" if status.startswith("ENDED") else STATE_NAME[status]
        out = {"session_id": sid, "state": state, "resume_text": self._resume_text(r["state"]) if state == "ACTIVE" else None}
        if state == "PAUSED_RISK":
            out["pause_reason"] = self._pause_reason(sid, r["state"])        # 정지가 아닌 상태에서는 생략(계약: null 또는 생략)
        return 200, out, {}

    def _pause_reason(self, sid: str, doc: Mapping) -> str:
        """정지 사유(CR-H002, ADR-0022). **현재 정지의** PAUSE 기록(세션 문서, 정본)만 읽는다. 알림 저장소를 보지 않으므로 알림 저장이 실패해도,
        이전 정지의 사유가 새 정지에 남지도 않는다. 사유가 없는 옛 기록은 reasons가 있으면 RISK, 없으면 OTHER."""
        for entry in reversed(doc["progress_record"]["pause_history"]):
            if entry.get("kind") == "PAUSE":
                return entry.get("pause_reason") or ("RISK" if entry.get("reasons") else "OTHER")
        return "OTHER"

    # ------------------------------------------------------------------ 직원 호출 재알림·실패 기록 (키오스크, 세션을 만든 기기)
    def _staff_call_common(self, body_type: str, req: Request) -> str:
        """공통 검증: 세션 ID 형식, 본문 스키마(turn_id 형식 포함), 세션이 있는가. turn_id를 돌려준다."""
        sid = req.params["sid"]
        _sid(sid)
        _check(body_type, req.body)
        status, _ = self._state_of(sid)
        if status == "ERROR":
            raise _err("UNAVAILABLE", "지금은 처리할 수 없다")
        if status is None:
            raise _err("NOT_FOUND", "모르는 세션이다")
        return req.body["turn_id"]

    @staticmethod
    def _staff_call_rejected(e) -> HttpError:
        if e.kind == "NO_OPEN_ALERT":
            return _err("CONFLICT", "열린 직원 알림이 없다", details={"reason": "NO_OPEN_ALERT"})
        if e.kind == "OUTCOME_MISMATCH":
            return _err("CONFLICT", "같은 turn_id에 다른 outcome이 왔다")
        if e.kind == "INTERVAL":
            return _err("UNAVAILABLE", "재알림이 너무 잦다", 429, {"Retry-After": str(max(1, e.retry_after_s))}, details={"reason": "INTERVAL"})
        return _err("UNAVAILABLE", "세션당 상한을 넘었다", 429, details={"reason": "RETRY_LIMIT"})        # Retry-After 없음(ADR-0021)

    def _staff_retry(self, req: Request):
        tid = self._staff_call_common("StaffCallRetryBody", req)
        try:
            return 200, self.alerts.retry_staff_call(req.params["sid"], tid), {}
        except self.orch.errors.StaffCallRejected as e:
            raise self._staff_call_rejected(e) from None
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "알림 저장소를 쓸 수 없다") from None

    def _staff_report(self, req: Request):
        tid = self._staff_call_common("StaffCallReportBody", req)
        try:
            return 200, self.alerts.report_manual_notice(req.params["sid"], tid, req.body["outcome"], "kiosk", getattr(self._tl, "user", None)), {}
        except self.orch.errors.StaffCallRejected as e:
            raise self._staff_call_rejected(e) from None
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "알림 저장소를 쓸 수 없다") from None       # 감사 기록을 못 남기면 보고도 남기지 않는다(한 트랜잭션)

    @staticmethod
    def _resume_text(doc: Mapping) -> Optional[str]:
        """직원 재개 뒤 이어서 할 안내 문장. 재개 항목 뒤로 발화 턴이 접수되면 null. 대화 로그에서 계산한다(재시작해도 같다)."""
        for entry in reversed(doc["dialogue_log"]):
            if entry["input"]["kind"] == "UTTERANCE":
                return None
            if entry["handling"] == {"handled": "RESUME"}:
                return entry["response_text"]
        return None

    # ------------------------------------------------------------------ 해결 표시
    def _resolve_if_applied(self, out, sid: str, decision: Optional[str], staff_id: Optional[str] = None) -> None:
        """개입 결정이 성공했으면 그 세션의 열린 알림을 해결한다(멱등: 같은 turn_id 재전송에서도 다시 해 본다)."""
        ir = out.orchestrator_output.get("intervention_result")
        if self.alerts is None or not ir or ir.get("result") != "APPLIED":
            return
        try:
            self.alerts.resolve(sid, decision, staff_id)
        except self.orch.errors.StoreError:
            log.error("알림 해결 표시를 남기지 못했다 session=%s", sid)

    def _resolve_ended(self, sid: str) -> None:
        """세션이 이탈로 끝나면 그 세션의 열린 알림은 더 직원이 할 일이 없다(결정 없이 해결)."""
        if self.alerts is not None:
            try:
                self.alerts.resolve(sid, None)
            except self.orch.errors.StoreError:
                log.error("알림 해결 표시를 남기지 못했다 session=%s", sid)

    # ------------------------------------------------------------------ 직원 알림 (staff)
    def _alerts(self, req: Request):
        q = req.query
        status = q.get("status")
        if status not in (None, "OPEN", "RESOLVED"):
            raise _err("INVALID_RECORD", "status는 OPEN 또는 RESOLVED여야 한다")
        try:
            limit = int(q.get("limit") or "50")
            cursor = q.get("cursor") or None
            if not 1 <= limit <= 200 or (cursor is not None and not cursor.isdigit()):
                raise ValueError
        except ValueError:
            raise _err("INVALID_RECORD", "limit(1~200) 또는 cursor가 잘못됐다") from None
        try:
            items, nxt = self.alerts.store.list(status, limit, cursor)
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "알림 저장소를 읽을 수 없다") from None
        return 200, {"items": items, "next_cursor": nxt}, {}

    # ------------------------------------------------------------------ 환자 데이터 조회 공통
    @staticmethod
    def _values(doc: Mapping) -> list:
        out = []
        for rec in doc["answer_records"]:
            cv = rec["current_value"]
            if isinstance(cv, dict) and "answer" in cv:
                out.append({"key": model_key(rec["slot"]), "value": cv["answer"],
                            "status": "CORRECTION" if rec.get("history") else "ANSWERED", "precision": rec.get("precision", "unspecified")})
        return out

    def _audit(self, role: str, sids: list, outcome: str, detail: Optional[str] = None) -> bool:
        """접근 기록을 남긴다. 못 남기면 False(호출자는 데이터를 주지 않는다)."""
        try:
            self.alerts.store.log_access_many(role, sids, outcome, detail, getattr(self._tl, 'user', None))
            return True
        except self.orch.errors.StoreError:
            log.error("접근 기록을 남기지 못해 조회를 거부했다 role=%s", role)
            return False

    _NO_AUDIT = ("UNAVAILABLE", "접근 기록을 남길 수 없어 조회하지 않았다")

    # ------------------------------------------------------------------ 직원 세션 조회 (staff)
    def _staff_session(self, req: Request):
        """StaffSessionView. 환자 데이터를 내보내므로 읽을 때마다 접근 기록(시각, 역할 이름, session_id)을 남긴다.
        기록을 남기지 못하면 데이터를 주지 않는다(기록 없는 열람을 만들지 않는다). 토큰 값·발화 내용은 기록에 없다."""
        sid, role = req.params["sid"], req.role
        _sid(sid)
        status, r = self._state_of(sid)
        if status == "ERROR":
            raise _err("UNAVAILABLE", "지금은 조회할 수 없다")
        if not self._audit(role, [sid], "OK" if r is not None and r["status"] == "OK" else "NOT_FOUND"):
            raise _err(*self._NO_AUDIT)
        if status is None:
            raise _err("NOT_FOUND", "모르는 세션이다")
        doc = r["state"]
        try:
            times = self.alerts.store.timeline(sid)
            alerts = self.alerts.store.alerts_of_session(sid)
            subject = self.alerts.store.subject_of(sid)
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "알림 저장소를 읽을 수 없다") from None
        turns = []
        for i, e in enumerate(doc["dialogue_log"]):
            t = times.get(i) or {}
            turns.append({"turn_index": i, "turn_id": t.get("turn_id"), "at": t.get("at"), "utterance": e["input"]["utterance"],
                          "response_text": e["response_text"], "action": (e.get("extract_output") or {}).get("action")})
        pr = self._pause_reason(sid, doc) if status == "PAUSED_RISK" else None
        view = {"session_id": sid, "subject_ref": subject, "status": status, "release_id": self.orch.store.release_id_of(sid),
                "created_at": (times.get(-1) or {}).get("at"), "turns": turns, "values": self._values(doc),
                "alerts": [{"alert_id": a["alert_id"], "kind": a["kind"], "status": a["status"],
                            "created_at": a["created_at"], "rule_id": a["rule_id"], "resolved_by": a["resolved_by"],
                            **{k: a[k] for k in ("pause_reason", "delivery_attempts", "last_delivery_status", "manual_notice_outcome",
                                                 "manual_notice_reported_at") if k in a}} for a in alerts]}
        if pr is not None:
            view["pause_reason"] = pr
        return 200, view, {}

    # ------------------------------------------------------------------ 결과 조회 (result-reader)
    def _result_of(self, sid: str, doc: Mapping, subject: Optional[str], times: Mapping) -> dict:
        prog = doc["progress_record"]
        log_ = doc["dialogue_log"]
        ended = prog["session_status"].startswith("ENDED")
        return {"session_id": sid, "subject_ref": subject, "status": prog["session_status"],
                "started_at": (times.get(-1) or {}).get("at"),
                "ended_at": (times.get(len(log_) - 1) or {}).get("at") if ended and log_ else None,
                "release_id": self.orch.store.release_id_of(sid),
                "question_bank_version": str(doc["session_info"]["question_bank_version"]),
                "answers": self._values(doc), "skipped_details": self.orch.blank_details(doc)}

    def _result(self, req: Request):
        """SessionResult(발화·대화 로그 없음, 부분 결과도 상태와 함께). 접근 기록 fail-closed."""
        sid, role = req.params["sid"], req.role
        _sid(sid)
        status, r = self._state_of(sid)
        if status == "ERROR":
            raise _err("UNAVAILABLE", "지금은 조회할 수 없다")
        if not self._audit(role, [sid], "OK" if status is not None else "NOT_FOUND"):
            raise _err(*self._NO_AUDIT)
        if status is None:
            raise _err("NOT_FOUND", "모르는 세션이다")
        try:
            body = self._result_of(sid, r["state"], self.alerts.store.subject_of(sid), self.alerts.store.timeline(sid))
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "알림 저장소를 읽을 수 없다") from None
        return 200, body, {}

    def _results(self, req: Request):
        role, q = req.role, req.query
        one = lambda k: q.get(k)
        subject, status, after, cursor = one("subject_ref"), one("status"), one("updated_after"), one("cursor")
        if subject is not None and not SUBJECT_REF.match(subject):
            raise _err("INVALID_RECORD", "subject_ref 형식이 아니다")
        if status is not None and status not in SESSION_STATUSES:
            raise _err("INVALID_RECORD", "status 값이 아니다")
        try:
            limit = int(one("limit") or "50")
            if not 1 <= limit <= 200:
                raise ValueError
            after_ts = None
            if after is not None:
                after_ts = datetime.fromisoformat(after.replace("Z", "+00:00")).timestamp()
            cur = None
            if cursor is not None:
                if not (cursor.isascii() and cursor.isdigit() and len(cursor) <= 18):         # 쓸 때마다 매기는 단조 증가 번호(U26, ADR-0034). 값은 처음 정수 문자열이다
                    raise ValueError
                cur = int(cursor)
        except (ValueError, OverflowError):
            raise _err("INVALID_RECORD", "limit(1~200), updated_after(ISO 8601), cursor가 잘못됐다") from None
        try:
            ids = self.alerts.store.sessions_of_subject(subject) if subject is not None else None
            rows = self.orch.store.list_sessions(status=status, updated_after=after_ts, session_ids=ids, limit=limit + 1, after=cur)
            items = []
            for sid, _st, _ts, _seq in rows[:limit]:
                r = self.orch.store.load(sid)
                if r["status"] == "OK":
                    items.append(self._result_of(sid, r["state"], self.alerts.store.subject_of(sid), self.alerts.store.timeline(sid)))
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "저장소를 읽을 수 없다") from None
        names = sorted(k for k, v in (("subject_ref", subject), ("status", status), ("updated_after", after), ("cursor", cursor)) if v is not None)
        if not self._audit(role, [i["session_id"] for i in items] or ["*"], "OK", "list " + ",".join(names)):     # 조건의 값(환자 참조)은 남기지 않는다
            raise _err(*self._NO_AUDIT)
        nxt = str(rows[limit - 1][3]) if len(rows) > limit else None
        return 200, {"items": items, "next_cursor": nxt}, {}

    # ------------------------------------------------------------------ 위험 신호 입력 (risk-source)
    def _risk_signal(self, req: Request):
        sid = req.params["sid"]
        _sid(sid)
        _check("RiskSignalBody", req.body)
        try:
            fresh, res = self.orch.apply_risk_signal(sid, req.body, alerts=self.alerts, signals=self.alerts.store)
        except self.orch.errors.SessionNotFound:
            raise _err("NOT_FOUND", "모르는 세션이다") from None
        except self.orch.errors.SessionEnded:
            raise _err("CONFLICT", "이미 끝난 세션이다") from None
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "지금은 처리할 수 없다") from None
        return (201 if fresh else 200), res, {}

    def _push_config(self, _req: Request):
        return 200, {"vapid_public_key": self.alerts.public_key()}, {}

    def _push_add(self, req: Request):
        body = req.body
        if self.push_allow_private and isinstance(body, dict) and isinstance(body.get("endpoint"), str):
            # 개발·시험 전용: 사설·내부 주소 문법 검사를 하지 않는다(스키마의 endpoint 문법은 SSRF 방어라 그 자리만 자리표시 주소로 바꿔 본다)
            ep = body["endpoint"]
            if not ep.startswith("https://") or len(ep) > 2048:
                raise _err("INVALID_RECORD", "endpoint는 https URL이어야 한다")
            _check("PushSubscribeBody", {**body, "endpoint": "https://push.example/"})
        else:
            _check("PushSubscribeBody", body)
            why = self.alerts.endpoint_error(body["endpoint"])      # 사설·내부 주소로 서버가 요청을 보내게 하지 못한다(SSRF, 감사 B6). 해석 검사는 보낼 때 한다
            if why:
                raise _err("INVALID_RECORD", why)
        ep, keys = body["endpoint"], body["keys"]
        sub = {"endpoint": ep, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}
        try:
            if self.alerts.store.count_subscriptions() >= self.max_subscriptions and not any(
                    s["endpoint"] == ep for s in self.alerts.store.subscriptions()):
                raise _err("CONFLICT", "구독 수 한도를 넘었다")
            self.alerts.store.upsert_subscription(sub)
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "알림 저장소에 쓸 수 없다") from None
        return 201, {"endpoint": ep}, {}

    def _push_unsub(self, req: Request):
        """해지: 본문 {"endpoint"} -> 204. 없는 endpoint도 204(멱등). 왜 본문: 푸시 엔드포인트 URL은 그 자체가 푸시 권한을 주는 값이라 URL에 싣지 않는다(계약 2.2.0)."""
        _check("PushUnsubscribeBody", req.body)
        try:
            self.alerts.store.remove_subscription(req.body["endpoint"])
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "알림 저장소에 쓸 수 없다") from None
        return 204, None, {}

    def _push_del(self, req: Request):
        log.warning("폐기 예정 방식 호출: DELETE /v1/staff/push-subscriptions?endpoint= (POST .../unsubscribe 본문으로 바꿀 것, 3.0.0에서 제거)")    # 엔드포인트 URL은 로그에 남기지 않는다
        ep = req.query.get("endpoint")
        if not ep:
            raise _err("INVALID_RECORD", "endpoint 쿼리가 필요하다")
        try:
            deleted = self.alerts.store.remove_subscription(ep)
        except self.orch.errors.StoreError:
            raise _err("UNAVAILABLE", "알림 저장소에 쓸 수 없다") from None
        return 200, {"endpoint": ep, "deleted": deleted}, {}

    # ------------------------------------------------------------------ 유휴 만료
    def expire_idle(self, session_ids) -> int:
        """유휴 세션을 사용자 이탈(SESSION_END)로 닫는다. 결과 전달이 불린다. 닫은 개수. 위험 정지 세션은 목록에 오지 않는다."""
        n = 0
        for sid in session_ids:
            try:
                out = self.orch.handle_turn(turn_id=EXPIRE_TURN_ID, frontend_message={"session_id": sid, "kind": "SESSION_END"})
                n += 0 if out.replayed else 1
                self._resolve_ended(sid)
            except Exception as e:
                log.error("유휴 세션을 닫지 못했다 error=%s", type(e).__name__)
        return n
