"""공개 API(I10): 키오스크 프론트 <-> 문진 서비스 (boundary-contracts/PUBLIC_API.md, D55, D58, D64, D66).

키오스크(토큰 역할 kiosk)
  POST /v1/sessions                    세션 시작   본문 {session_id, kind: SESSION_START}
  POST /v1/sessions/{id}/turns         환자 발화   본문 {session_id, kind: UTTERANCE, utterance} + 봉투 turn_id (멱등)
  POST /v1/sessions/{id}/end           세션 종료   본문 {session_id, kind: SESSION_END}
  GET  /v1/sessions/{id}/state         세션 상태   SessionState (위험 정지 중 폴링, 재개하면 resume_text)
직원 콘솔(토큰 역할 staff)
  POST /v1/sessions/{id}/intervention  개입 결정   본문 {session_id, decision: RESUME|END} + 봉투 turn_id (멈춘 세션만)
  GET  /v1/staff/alerts                알림 목록   StaffAlertPage
  GET  /v1/staff/sessions/{id}         세션 기록 조회   StaffSessionView (대화 기록, 저장된 값, 알림. 읽을 때마다 접근 기록)
  GET  /v1/staff/alerts/stream         알림 SSE    (스트리밍은 service.py가 한다)
  GET  /v1/staff/push-config           푸시 설정   PushConfig (공개키 또는 null)
  POST/DELETE /v1/staff/push-subscriptions  웹 푸시 구독 등록·해지
`turn_id`는 API 봉투 필드다: 본문에서 떼어 낸 나머지를 문진 스키마(FrontendMessage, InterventionDecision)로 검증한다(D66).
응답은 FrontendInput(턴), 위 스키마(조회). 오류는 BoundaryError 본문과 상태 코드(401·403 PERMISSION_DENIED, 404 NOT_FOUND,
409 CONFLICT, 429·503 UNAVAILABLE + Retry-After). 문서가 정하지 않은 점의 해석은 docs/change-requests/CR-009.

이 모듈은 HTTP를 모른다(handle이 상태 코드와 본문을 돌려준다). 연결은 service.py가 한다.
"""
from __future__ import annotations

import collections
import hmac
import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Mapping, Optional
from urllib.parse import parse_qs

import jsonschema

import survey_schema
from boundary_contracts import PIPELINE_SCHEMA_VERSION

from .alerts import KIND_MAP, AlertService
from .store.effects import model_key
from . import netguard
from .errors import SessionEnded, SessionNotFound, StoreError, TurnIdConflict, TurnInProgress
from .orchestrator import Orchestrator

log = logging.getLogger("survey_service.api")

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TURN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_BODY = 64 * 1024
HTTP_CODE = {"INVALID_RECORD": 400, "PERMISSION_DENIED": 401, "NOT_FOUND": 404, "CONFLICT": 409, "UNAVAILABLE": 503}
STAFF_ROUTES = {"intervention", "staff_session", "alerts", "push_add", "push_del", "push_config"}
ROLE_OF_ROUTE = {"result": "result-reader", "results": "result-reader", "risk_signal": "risk-source"}     # 그 밖의 직원 경로는 staff, 나머지는 kiosk
SUBJECT_REF = re.compile(r"^[A-Za-z0-9._:/#=+-]{1,128}$")
STAFF_ID = re.compile(r"^[A-Za-z0-9._:@/#=+-]{1,128}$")
SESSION_STATUSES = ("IN_PROGRESS", "PAUSED_RISK", "ENDED_COMPLETE", "ENDED_STAFF", "ENDED_LEFT", "ENDED_USER_STOP")
START_TURN_ID, END_TURN_ID, EXPIRE_TURN_ID = "start", "end", "idle-expire"   # turn_id가 없는 동작의 고정 ID(CR-009)
RESERVED = (START_TURN_ID, END_TURN_ID, EXPIRE_TURN_ID)
STATE_NAME = {"IN_PROGRESS": "ACTIVE", "PAUSED_RISK": "PAUSED_RISK"}


def _err(code: str, message: str, status: Optional[int] = None, headers: Optional[dict] = None, details: Optional[dict] = None):
    rec = {"pipeline_schema_version": PIPELINE_SCHEMA_VERSION, "code": code, "message": message,
           "retryable": code == "UNAVAILABLE"}
    if details:
        rec["details"] = details
    return status or HTTP_CODE[code], rec, headers or {}


USER_ID = re.compile(r"^[A-Za-z0-9._@:+-]{1,128}$")      # X-Openit-User: gateway가 인증한 계정 ID. 형식이 어긋나면 개인 ID 없이 역할만 기록한다


def _has_surrogate(x) -> bool:
    if isinstance(x, str):
        try:
            x.encode("utf-8")
            return False
        except UnicodeEncodeError:
            return True
    if isinstance(x, dict):
        return any(_has_surrogate(k) or _has_surrogate(v) for k, v in x.items())
    if isinstance(x, list):
        return any(_has_surrogate(v) for v in x)
    return False


class PublicApi:
    def __init__(self, orchestrator: Orchestrator, token: str, *, staff_token: Optional[str] = None, max_inflight: int = 32,
                 max_utterance_chars: int = 1000, alerts: Optional[AlertService] = None, max_subscriptions: int = 100,
                 retry_after_seconds: int = 1, push_allow_private: bool = False, state_poll_min_interval: float = 0.5, result_token: Optional[str] = None,
                 risk_token: Optional[str] = None, require_subject_ref: bool = False, device_tokens: Optional[dict] = None) -> None:
        self.device_tokens = dict(device_tokens or {})        # {device_id: 토큰}. 세션을 만든 기기의 토큰으로만 그 세션에 접근한다 (D88 U8, ADR-0016)
        if not token and not self.device_tokens:
            raise ValueError("공개 API 토큰이 비어 있다")
        token = token or None
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
        self._msg = jsonschema.Draft202012Validator(survey_schema.load_schema("FrontendMessage"))
        self._dec = jsonschema.Draft202012Validator(survey_schema.load_schema("InterventionDecision"))
        self._sig = jsonschema.Draft202012Validator(survey_schema.load_schema("RiskSignal"))

    # ------------------------------------------------------------------ 진입
    def handle(self, method: str, path: str, auth: str, raw: bytes, user: Optional[str] = None) -> tuple[int, dict, dict]:
        """(상태 코드, 본문, 추가 헤더). /healthz, /readyz는 service.py가 처리한다. 429·503에는 Retry-After(초, 정수)를 붙인다.
        user는 게이트웨이가 인증한 계정 ID(X-Openit-User 헤더 값). 직원 개입에서만 쓴다."""
        self._tl.t0 = time.monotonic()                      # 턴 총 마감은 요청을 받은 때부터 센다
        self._tl.user = user if isinstance(user, str) and USER_ID.match(user) else None       # 접근 기록의 개인 ID(D88 U7). gateway가 인증해 붙인 헤더만 믿는다
        status, body, headers = self._handle(method, path, auth, raw, user)
        self._record(method, path, status)
        if status in (429, 503) and "Retry-After" not in headers:
            headers = {**headers, "Retry-After": str(self.retry_after)}
        return status, body, headers

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

    def _handle(self, method: str, path: str, auth: str, raw: bytes, user: Optional[str] = None):
        path, _, query = path.partition("?")
        route = self._route(method, path)
        if route is None:
            return _err("NOT_FOUND", "경로가 없다")
        want = ROLE_OF_ROUTE.get(route[0], "staff" if route[0] in STAFF_ROUTES else "kiosk")
        if (want == "result-reader" and self.result_token is None) or (want == "risk-source" and self.risk_token is None):
            return _err("NOT_FOUND", "경로가 없다")                  # 그 역할의 토큰을 설정하지 않았으면 기능이 꺼진 것이다
        role = self._role(auth)
        if role is None:
            return _err("PERMISSION_DENIED", "토큰이 없거나 틀리다", 401, {"WWW-Authenticate": "Bearer"})
        if want != role:                                           # 역할마다 자기 경로만
            return _err("PERMISSION_DENIED", "이 토큰의 역할로는 호출할 수 없다", 403)
        if len(raw) > MAX_BODY:
            return _err("INVALID_RECORD", "본문이 너무 크다")
        body = None
        if method == "POST":
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                return _err("INVALID_RECORD", "본문이 JSON이 아니다")
            if not isinstance(body, dict):
                return _err("INVALID_RECORD", "본문이 JSON 객체가 아니다")
            if _has_surrogate(body):          # UTF-8로 인코딩할 수 없는 문자(짝 없는 서로게이트): 저장·전송에서 터지기 전에 400
                return _err("INVALID_RECORD", "본문에 올바르지 않은 유니코드 문자가 있다")
        if (route[0] in STAFF_ROUTES - {"intervention"} or route[0] in ROLE_OF_ROUTE) and self.alerts is None:
            return _err("NOT_FOUND", "경로가 없다")
        if not self._slots.acquire(blocking=False):
            return _err("UNAVAILABLE", "요청이 너무 많다", 429)
        try:
            name, arg = route
            if role == "kiosk" and name in ("state", "turn", "end"):
                denied = self._device_denied(arg)
                if denied:
                    return denied
            if role == "kiosk" and name == "start" and isinstance(body, dict):
                denied = self._device_denied(body.get("session_id") if isinstance(body.get("session_id"), str) else None)
                if denied:
                    return denied
            if name in ("alerts", "push_del"):
                return getattr(self, "_" + name)(parse_qs(query))
            if name in ("state", "push_config"):
                return getattr(self, "_" + name)(arg)
            if name == "staff_session":
                return self._staff_session(arg, role)
            if name == "result":
                return self._result(arg, role)
            if name == "results":
                return self._results(parse_qs(query), role)
            if name == "intervention":
                return self._intervention(body, arg, user)
            if name == "risk_signal":
                return self._risk_signal(body, arg)
            return getattr(self, "_" + name)(body, arg)
        finally:
            self._slots.release()

    def _role(self, auth: str) -> Optional[str]:
        if not auth.startswith("Bearer "):
            return None
        tok = auth[7:].encode()
        found = None
        self._tl.device = None
        for role, t in (("kiosk", self.token), ("staff", self.staff_token), ("result-reader", self.result_token), ("risk-source", self.risk_token)):
            if t is not None and hmac.compare_digest(tok, t.encode()):       # 모든 토큰을 끝까지 비교한다
                found = role
        for dev, t in self.device_tokens.items():
            if hmac.compare_digest(tok, t.encode()):
                found, self._tl.device = "kiosk", dev
        return found

    def _device_denied(self, sid: Optional[str]):
        """기기별 토큰(U8): 세션을 만든 기기 토큰으로만 접근한다. 다른 기기 토큰(또는 공유 토큰)이면 세션이 없는 것처럼 404. 세션에 기기가 기록돼 있지 않으면(공유 토큰으로 만든 세션) 막지 않는다."""
        if self.alerts is None or not sid or not self.device_tokens:         # 기기별 토큰을 쓰지 않는 구성(공유 토큰만)은 확인하지 않는다(하위 호환)
            return None
        try:
            owner = self.alerts.store.device_of(sid)
        except StoreError:
            return _err("UNAVAILABLE", "지금은 처리할 수 없다")
        if owner is not None and owner != getattr(self._tl, "device", None):
            log.warning("다른 기기의 세션 접근을 거부했다 route_session=%s", "*")
            return _err("NOT_FOUND", "모르는 세션이다")
        return None

    @staticmethod
    def _route(method: str, path: str):
        if method == "GET" and path == "/v1/staff/alerts":
            return ("alerts", None)
        m = re.fullmatch(r"/v1/results/([^/]+)", path)
        if method == "GET" and m:
            return ("result", m.group(1))
        if method == "GET" and path == "/v1/results":
            return ("results", None)
        m = re.fullmatch(r"/v1/sessions/([^/]+)/risk-signals", path)
        if method == "POST" and m:
            return ("risk_signal", m.group(1))
        m = re.fullmatch(r"/v1/staff/sessions/([^/]+)", path)
        if method == "GET" and m:
            return ("staff_session", m.group(1))
        if method == "GET" and path == "/v1/staff/push-config":
            return ("push_config", None)
        if method == "POST" and path == "/v1/staff/push-subscriptions":
            return ("push_add", None)
        if method == "DELETE" and path == "/v1/staff/push-subscriptions":
            return ("push_del", None)
        if method == "POST" and path == "/v1/sessions":
            return ("start", None)
        m = re.fullmatch(r"/v1/sessions/([^/]+)/(turns|end|intervention)", path)
        if method == "POST" and m:
            return ({"turns": "turn", "end": "end", "intervention": "intervention"}[m.group(2)], m.group(1))
        m = re.fullmatch(r"/v1/sessions/([^/]+)/state", path)
        if method == "GET" and m:
            return ("state", m.group(1))
        return None

    # ------------------------------------------------------------------ 검증
    @staticmethod
    def _envelope(body: Mapping, required: bool, reserved_ok: bool):
        """봉투 필드 turn_id를 떼어 낸다. (나머지 본문, turn_id | None, 오류 응답 | None)."""
        rest = dict(body)
        if "turn_id" not in rest:
            return rest, None, (_err("INVALID_RECORD", "turn_id가 필요하다") if required else None)
        tid = rest.pop("turn_id")
        if not isinstance(tid, str) or not TURN_ID.match(tid):
            return rest, None, _err("INVALID_RECORD", "turn_id는 영문·숫자 등 1~128자 문자열이어야 한다")
        if tid in RESERVED and not reserved_ok:
            return rest, None, _err("INVALID_RECORD", f"turn_id {tid}는 서버가 쓰는 예약 값이다")
        return rest, tid, None

    def _message(self, rest: Mapping, path_sid: Optional[str], kind: str):
        """봉투를 뗀 본문을 FrontendMessage로 검증한다. (메시지 | None, 오류 응답 | None)."""
        sid = rest.get("session_id")
        if not isinstance(sid, str) or not SESSION_ID.match(sid):
            return None, _err("INVALID_RECORD", "session_id는 영문·숫자·-_ 1~64자여야 한다")
        if path_sid is not None and path_sid != sid:
            return None, _err("INVALID_RECORD", "경로의 세션 ID와 본문의 session_id가 다르다")
        if rest.get("kind") != kind:
            return None, _err("INVALID_RECORD", f"kind는 {kind}여야 한다")
        if list(self._msg.iter_errors(rest)):
            return None, _err("INVALID_RECORD", "FrontendMessage 형식이 아니다(모르는 필드 포함)")
        if kind == "UTTERANCE":
            u = rest["utterance"]
            if not u.strip():
                return None, _err("INVALID_RECORD", "utterance가 비었다")
            if len(u) > self.max_chars:
                return None, _err("INVALID_RECORD", f"utterance가 {self.max_chars}자를 넘는다")
        return dict(rest), None

    # ------------------------------------------------------------------ 동작
    def _state_of(self, sid: str):
        r = self.orch.store.load(sid)
        if r["status"] == "DB_ERROR":
            return "ERROR", None
        return (r["state"]["progress_record"]["session_status"] if r["status"] == "OK" else None), r

    def _run(self, sid: str, turn_id: str, msg: Mapping):
        try:
            out = self.orch.handle_turn(turn_id=turn_id, frontend_message=msg, started=getattr(self._tl, 't0', None))
        except TurnIdConflict:
            return _err("CONFLICT", "같은 turn_id에 다른 입력이 왔다")
        except TurnInProgress:
            return self._in_progress()
        return 200, out.frontend_input or {"response_text": None, "turn_failure_signal": None}, {}

    def _in_progress(self):
        """같은 turn_id가 처리 중. `code: CONFLICT`(409), `details.reason: TURN_IN_PROGRESS`, `Retry-After` 헤더(HTTP.md의 409 사유 규약, CR-015 처리됨).
        입력 불일치 409(details 없음)와 구분된다."""
        return _err("CONFLICT", "같은 turn_id가 처리 중이다. Retry-After 뒤 같은 turn_id로 다시 보낸다", 409,
                    {"Retry-After": str(self.retry_after)}, details={"reason": "TURN_IN_PROGRESS"})

    def _replayed(self, sid: str, turn_id: str) -> bool:
        try:
            return self.orch.store.get_turn(sid, turn_id) is not None
        except Exception:
            return False

    def _start(self, body: Mapping, _sid):
        rest, tid, err = self._envelope(body, required=False, reserved_ok=True)
        if err:
            return err
        rest = dict(rest)
        subject = rest.pop("subject_ref", None)             # 환자 참조는 문진 메시지(FrontendMessage)가 아니라 세션 메타다
        if subject is not None and (not isinstance(subject, str) or not SUBJECT_REF.match(subject)):
            return _err("INVALID_RECORD", "subject_ref는 [A-Za-z0-9._:/#=+-] 1~128자 문자열이어야 한다")
        msg, err = self._message(rest, None, "SESSION_START")
        if err:
            return err
        sid, tid = msg["session_id"], tid or START_TURN_ID
        if not self._replayed(sid, tid):
            status, _ = self._state_of(sid)
            if status == "ERROR" or (status is None and self.orch.releases.active() is None):
                return _err("UNAVAILABLE", "지금은 세션을 시작할 수 없다")
            if status and status.startswith("ENDED"):
                return _err("CONFLICT", "이미 끝난 세션이다. 새 세션 ID로 시작해야 한다")
            if status is None and subject is None and self.require_subject_ref:
                return _err("INVALID_RECORD", "subject_ref가 필요하다")
            if status is None and self.alerts is not None:
                try:
                    self.alerts.store.set_subject(sid, subject, getattr(self._tl, 'device', None))       # 세션을 만들기 전에 남긴다(환자 참조, 만든 기기): 못 남기면 시작하지 않는다
                except StoreError:
                    return _err("UNAVAILABLE", "세션 메타를 저장할 수 없다")
        return self._run(sid, tid, msg)

    def _turn(self, body: Mapping, path_sid: str):
        rest, tid, err = self._envelope(body, required=True, reserved_ok=False)
        if err:
            return err
        msg, err = self._message(rest, path_sid, "UTTERANCE")
        if err:
            return err
        sid = msg["session_id"]
        if not self._replayed(sid, tid):
            status, _ = self._state_of(sid)
            if status == "ERROR":
                return _err("UNAVAILABLE", "지금은 처리할 수 없다")
            if status is None:
                return _err("NOT_FOUND", "모르는 세션이다. 먼저 세션을 시작해야 한다")
            if status.startswith("ENDED"):
                return _err("CONFLICT", "이미 끝난 세션이다")
        return self._run(sid, tid, msg)

    def _end(self, body: Mapping, path_sid: str):
        rest, tid, err = self._envelope(body, required=False, reserved_ok=True)
        if err:
            return err
        msg, err = self._message(rest, path_sid, "SESSION_END")
        if err:
            return err
        sid, tid = msg["session_id"], tid or END_TURN_ID
        if not self._replayed(sid, tid):
            status, _ = self._state_of(sid)
            if status == "ERROR":
                return _err("UNAVAILABLE", "지금은 처리할 수 없다")
            if status is None:
                return _err("NOT_FOUND", "모르는 세션이다")
            if status.startswith("ENDED"):                 # 이미 끝난 세션의 종료는 같은 결과(말할 것 없음)
                return 200, {"response_text": None, "turn_failure_signal": None}, {}
        res = self._run(sid, tid, msg)
        if res[0] == 200:
            self._resolve_ended(sid)
        return res

    def _intervention(self, body: Mapping, path_sid: str, user: Optional[str] = None):
        if not user:
            return _err("PERMISSION_DENIED", "X-Openit-User(직원 계정 ID)가 없다", 401, {"WWW-Authenticate": "Bearer"})
        if not STAFF_ID.match(user):
            return _err("INVALID_RECORD", "X-Openit-User 형식이 아니다")
        rest, tid, err = self._envelope(body, required=True, reserved_ok=False)
        if err:
            return err
        rest = {k: v for k, v in rest.items() if k != "staff_id"}        # 본문의 staff_id는 믿지 않는다(헤더만)
        sid = rest.get("session_id")
        if not isinstance(sid, str) or not SESSION_ID.match(sid):
            return _err("INVALID_RECORD", "session_id는 영문·숫자·-_ 1~64자여야 한다")
        if sid != path_sid:
            return _err("INVALID_RECORD", "경로의 세션 ID와 본문의 session_id가 다르다")
        if list(self._dec.iter_errors(rest)):
            return _err("INVALID_RECORD", "InterventionDecision 형식이 아니다(decision은 RESUME 또는 END)")
        if not self._replayed(sid, tid):
            status, _ = self._state_of(sid)
            if status == "ERROR":
                return _err("UNAVAILABLE", "지금은 처리할 수 없다")
            if status is None:
                return _err("NOT_FOUND", "모르는 세션이다")
            if status != "PAUSED_RISK":
                return _err("CONFLICT", "멈춘 세션이 아니다")
        try:
            out = self.orch.handle_turn(turn_id=tid, intervention_decision=dict(rest), staff_id=user, started=getattr(self._tl, 't0', None))
        except TurnIdConflict:
            return _err("CONFLICT", "같은 turn_id에 다른 입력이 왔다")
        except TurnInProgress:
            return self._in_progress()
        self._resolve_if_applied(out, sid, rest["decision"], user)
        return 200, out.frontend_input or {"response_text": None, "turn_failure_signal": None}, {}

    # ------------------------------------------------------------------ 세션 상태 (키오스크 폴링)
    def _state(self, sid: str):
        if not SESSION_ID.match(sid):
            return _err("INVALID_RECORD", "session_id는 영문·숫자·-_ 1~64자여야 한다")
        if self.state_poll_min_interval > 0:                  # 폴링 빈도 상한(세션마다)
            now = time.monotonic()
            with self._poll_lock:
                last = self._polls.get(sid)
                if last is not None and now - last < self.state_poll_min_interval:
                    return _err("UNAVAILABLE", "상태 조회가 너무 잦다", 429)
                self._polls[sid] = now
                if len(self._polls) > 2000:
                    self._polls = {k: v for k, v in self._polls.items() if now - v < 60}
        status, r = self._state_of(sid)
        if status == "ERROR":
            return _err("UNAVAILABLE", "지금은 조회할 수 없다")
        if status is None:
            return _err("NOT_FOUND", "모르는 세션이다")
        state = "ENDED" if status.startswith("ENDED") else STATE_NAME[status]
        return 200, {"session_id": sid, "state": state,
                     "resume_text": self._resume_text(r["state"]) if state == "ACTIVE" else None}, {}

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
        except StoreError:
            log.error("알림 해결 표시를 남기지 못했다 session=%s", sid)

    def _resolve_ended(self, sid: str) -> None:
        """세션이 이탈로 끝나면 그 세션의 열린 알림은 더 직원이 할 일이 없다(결정 없이 해결)."""
        if self.alerts is not None:
            try:
                self.alerts.resolve(sid, None)
            except StoreError:
                log.error("알림 해결 표시를 남기지 못했다 session=%s", sid)

    # ------------------------------------------------------------------ 직원 알림 (staff)
    def _alerts(self, q: dict):
        status = (q.get("status") or [None])[0]
        if status not in (None, "OPEN", "RESOLVED"):
            return _err("INVALID_RECORD", "status는 OPEN 또는 RESOLVED여야 한다")
        try:
            limit = int((q.get("limit") or ["50"])[0])
            cursor = (q.get("cursor") or [None])[0]
            if not 1 <= limit <= 200 or (cursor is not None and not cursor.isdigit()):
                raise ValueError
        except ValueError:
            return _err("INVALID_RECORD", "limit(1~200) 또는 cursor가 잘못됐다")
        try:
            items, nxt = self.alerts.store.list(status, limit, cursor)
        except StoreError:
            return _err("UNAVAILABLE", "알림 저장소를 읽을 수 없다")
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
        except StoreError:
            log.error("접근 기록을 남기지 못해 조회를 거부했다 role=%s", role)
            return False

    _NO_AUDIT = ("UNAVAILABLE", "접근 기록을 남길 수 없어 조회하지 않았다")

    # ------------------------------------------------------------------ 직원 세션 조회 (staff)
    def _staff_session(self, sid: str, role: str):
        """StaffSessionView. 환자 데이터를 내보내므로 읽을 때마다 접근 기록(시각, 역할 이름, session_id)을 남긴다.
        기록을 남기지 못하면 데이터를 주지 않는다(기록 없는 열람을 만들지 않는다). 토큰 값·발화 내용은 기록에 없다."""
        if not SESSION_ID.match(sid):
            return _err("INVALID_RECORD", "session_id는 영문·숫자·-_ 1~64자여야 한다")
        status, r = self._state_of(sid)
        if status == "ERROR":
            return _err("UNAVAILABLE", "지금은 조회할 수 없다")
        if not self._audit(role, [sid], "OK" if r is not None and r["status"] == "OK" else "NOT_FOUND"):
            return _err(*self._NO_AUDIT)
        if status is None:
            return _err("NOT_FOUND", "모르는 세션이다")
        doc = r["state"]
        try:
            times = self.alerts.store.timeline(sid)
            alerts = self.alerts.store.alerts_of_session(sid)
            subject = self.alerts.store.subject_of(sid)
        except StoreError:
            return _err("UNAVAILABLE", "알림 저장소를 읽을 수 없다")
        turns = []
        for i, e in enumerate(doc["dialogue_log"]):
            t = times.get(i) or {}
            turns.append({"turn_index": i, "turn_id": t.get("turn_id"), "at": t.get("at"), "utterance": e["input"]["utterance"],
                          "response_text": e["response_text"], "action": (e.get("extract_output") or {}).get("action")})
        kinds = {v: k for k, v in KIND_MAP.items()}
        view = {"session_id": sid, "subject_ref": subject, "status": status, "release_id": self.orch.store.release_id_of(sid),
                "created_at": (times.get(-1) or {}).get("at"), "turns": turns, "values": self._values(doc),
                "alerts": [{"alert_id": a["alert_id"], "kind": kinds.get(a["kind"], a["kind"]), "status": a["status"],
                            "created_at": a["created_at"], "rule_id": a["rule_id"], "resolved_by": a["resolved_by"]} for a in alerts]}
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
                "answers": self._values(doc), "skipped": [e["question_id"] for e in prog["skipped"]]}

    def _result(self, sid: str, role: str):
        """SessionResult(발화·대화 로그 없음, 부분 결과도 상태와 함께). 접근 기록 fail-closed."""
        if not SESSION_ID.match(sid):
            return _err("INVALID_RECORD", "session_id는 영문·숫자·-_ 1~64자여야 한다")
        status, r = self._state_of(sid)
        if status == "ERROR":
            return _err("UNAVAILABLE", "지금은 조회할 수 없다")
        if not self._audit(role, [sid], "OK" if status is not None else "NOT_FOUND"):
            return _err(*self._NO_AUDIT)
        if status is None:
            return _err("NOT_FOUND", "모르는 세션이다")
        try:
            body = self._result_of(sid, r["state"], self.alerts.store.subject_of(sid), self.alerts.store.timeline(sid))
        except StoreError:
            return _err("UNAVAILABLE", "알림 저장소를 읽을 수 없다")
        return 200, body, {}

    def _results(self, q: dict, role: str):
        one = lambda k: (q.get(k) or [None])[0]
        subject, status, after, cursor = one("subject_ref"), one("status"), one("updated_after"), one("cursor")
        if subject is not None and not SUBJECT_REF.match(subject):
            return _err("INVALID_RECORD", "subject_ref 형식이 아니다")
        if status is not None and status not in SESSION_STATUSES:
            return _err("INVALID_RECORD", "status 값이 아니다")
        try:
            limit = int(one("limit") or "50")
            if not 1 <= limit <= 200:
                raise ValueError
            after_ts = None
            if after is not None:
                after_ts = datetime.fromisoformat(after.replace("Z", "+00:00")).timestamp()
            cur = None
            if cursor is not None:
                ts, _, csid = cursor.partition("|")
                cur = (float(ts), csid)
                if not SESSION_ID.match(csid):
                    raise ValueError
        except (ValueError, OverflowError):
            return _err("INVALID_RECORD", "limit(1~200), updated_after(ISO 8601), cursor가 잘못됐다")
        try:
            ids = self.alerts.store.sessions_of_subject(subject) if subject is not None else None
            rows = self.orch.store.list_sessions(status=status, updated_after=after_ts, session_ids=ids, limit=limit + 1, after=cur)
            items = []
            for sid, _st, _ts in rows[:limit]:
                r = self.orch.store.load(sid)
                if r["status"] == "OK":
                    items.append(self._result_of(sid, r["state"], self.alerts.store.subject_of(sid), self.alerts.store.timeline(sid)))
        except StoreError:
            return _err("UNAVAILABLE", "저장소를 읽을 수 없다")
        names = sorted(k for k, v in (("subject_ref", subject), ("status", status), ("updated_after", after), ("cursor", cursor)) if v is not None)
        if not self._audit(role, [i["session_id"] for i in items] or ["*"], "OK", "list " + ",".join(names)):     # 조건의 값(환자 참조)은 남기지 않는다
            return _err(*self._NO_AUDIT)
        nxt = f"{rows[limit - 1][2]!r}|{rows[limit - 1][0]}" if len(rows) > limit else None
        return 200, {"items": items, "next_cursor": nxt}, {}

    # ------------------------------------------------------------------ 위험 신호 입력 (risk-source)
    def _risk_signal(self, body: Mapping, sid: str):
        if not SESSION_ID.match(sid):
            return _err("INVALID_RECORD", "session_id는 영문·숫자·-_ 1~64자여야 한다")
        if list(self._sig.iter_errors(body)):
            return _err("INVALID_RECORD", "RiskSignal 형식이 아니다(signal_id, source, code 필요. 모르는 필드 불가)")
        try:
            fresh, res = self.orch.apply_risk_signal(sid, body, alerts=self.alerts, signals=self.alerts.store)
        except SessionNotFound:
            return _err("NOT_FOUND", "모르는 세션이다")
        except SessionEnded:
            return _err("CONFLICT", "이미 끝난 세션이다")
        except StoreError:
            return _err("UNAVAILABLE", "지금은 처리할 수 없다")
        return (201 if fresh else 200), res, {}

    def _push_config(self, _arg):
        return 200, {"vapid_public_key": self.alerts.public_key()}, {}

    def _push_add(self, body: Mapping, _sid):
        ep, keys = body.get("endpoint"), body.get("keys")
        if not isinstance(ep, str) or not ep.startswith("https://") or len(ep) > 2048:
            return _err("INVALID_RECORD", "endpoint는 https URL이어야 한다")
        if not self.push_allow_private:
            why = netguard.endpoint_syntax_error(ep)             # 사설·내부 주소로 서버가 요청을 보내게 하지 못한다(SSRF, 감사 B6). 해석 검사는 보낼 때 한다
            if why:
                return _err("INVALID_RECORD", why)
        if not isinstance(keys, dict) or not all(isinstance(keys.get(k), str) and keys.get(k) for k in ("p256dh", "auth")):
            return _err("INVALID_RECORD", "keys.p256dh와 keys.auth가 있어야 한다")
        unknown = set(body) - {"endpoint", "keys", "expirationTime"}
        if unknown:
            return _err("INVALID_RECORD", f"알 수 없는 필드: {sorted(unknown)}")
        sub = {"endpoint": ep, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}
        try:
            if self.alerts.store.count_subscriptions() >= self.max_subscriptions and not any(
                    s["endpoint"] == ep for s in self.alerts.store.subscriptions()):
                return _err("CONFLICT", "구독 수 한도를 넘었다")
            self.alerts.store.upsert_subscription(sub)
        except StoreError:
            return _err("UNAVAILABLE", "알림 저장소에 쓸 수 없다")
        return 201, {"endpoint": ep}, {}

    def _push_del(self, q: dict):
        ep = (q.get("endpoint") or [None])[0]
        if not ep:
            return _err("INVALID_RECORD", "endpoint 쿼리가 필요하다")
        try:
            deleted = self.alerts.store.remove_subscription(ep)
        except StoreError:
            return _err("UNAVAILABLE", "알림 저장소에 쓸 수 없다")
        return 200, {"endpoint": ep, "deleted": deleted}, {}

    def stream_auth_error(self, auth: str):
        """SSE 연결 전 확인. 문제가 없으면 None, 있으면 (상태, 본문, 헤더)."""
        if self.alerts is None:
            return _err("NOT_FOUND", "경로가 없다")
        role = self._role(auth)
        if role is None:
            return _err("PERMISSION_DENIED", "토큰이 없거나 틀리다", 401, {"WWW-Authenticate": "Bearer"})
        if role != "staff":
            return _err("PERMISSION_DENIED", "이 토큰의 역할로는 호출할 수 없다", 403)
        return None

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
