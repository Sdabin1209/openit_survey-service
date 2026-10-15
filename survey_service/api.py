"""공개 API(I10): 키오스크 프론트 <-> 문진 서비스 (boundary-contracts/PUBLIC_API.md, D55, D58, D64, D66).

키오스크(토큰 역할 kiosk)
  POST /v1/sessions                    세션 시작   본문 {session_id, kind: SESSION_START}
  POST /v1/sessions/{id}/turns         환자 발화   본문 {session_id, kind: UTTERANCE, utterance} + 봉투 turn_id (멱등)
  POST /v1/sessions/{id}/end           세션 종료   본문 {session_id, kind: SESSION_END}
  GET  /v1/sessions/{id}/state         세션 상태   SessionState (위험 정지 중 폴링, 재개하면 resume_text)
직원 콘솔(토큰 역할 staff)
  POST /v1/sessions/{id}/intervention  개입 결정   본문 {session_id, decision: RESUME|END} + 봉투 turn_id (멈춘 세션만)
  GET  /v1/staff/alerts                알림 목록   StaffAlertPage
  GET  /v1/staff/alerts/stream         알림 SSE    (스트리밍은 service.py가 한다)
  GET  /v1/staff/push-config           푸시 설정   PushConfig (공개키 또는 null)
  POST/DELETE /v1/staff/push-subscriptions  웹 푸시 구독 등록·해지
`turn_id`는 API 봉투 필드다: 본문에서 떼어 낸 나머지를 문진 스키마(FrontendMessage, InterventionDecision)로 검증한다(D66).
응답은 FrontendInput(턴), 위 스키마(조회). 오류는 BoundaryError 본문과 상태 코드(401·403 PERMISSION_DENIED, 404 NOT_FOUND,
409 CONFLICT, 429·503 UNAVAILABLE + Retry-After). 문서가 정하지 않은 점의 해석은 docs/change-requests/CR-009.

이 모듈은 HTTP를 모른다(handle이 상태 코드와 본문을 돌려준다). 연결은 service.py가 한다.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
from typing import Mapping, Optional
from urllib.parse import parse_qs

import jsonschema

import survey_schema
from boundary_contracts import PIPELINE_SCHEMA_VERSION

from .alerts import AlertService
from .errors import StoreError, TurnIdConflict
from .orchestrator import Orchestrator

log = logging.getLogger("survey_service.api")

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TURN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_BODY = 64 * 1024
HTTP_CODE = {"INVALID_RECORD": 400, "PERMISSION_DENIED": 401, "NOT_FOUND": 404, "CONFLICT": 409, "UNAVAILABLE": 503}
STAFF_ROUTES = {"intervention", "alerts", "push_add", "push_del", "push_config"}
START_TURN_ID, END_TURN_ID, EXPIRE_TURN_ID = "start", "end", "idle-expire"   # turn_id가 없는 동작의 고정 ID(CR-009)
RESERVED = (START_TURN_ID, END_TURN_ID, EXPIRE_TURN_ID)
STATE_NAME = {"IN_PROGRESS": "ACTIVE", "PAUSED_RISK": "PAUSED_RISK"}


def _err(code: str, message: str, status: Optional[int] = None, headers: Optional[dict] = None):
    rec = {"pipeline_schema_version": PIPELINE_SCHEMA_VERSION, "code": code, "message": message,
           "retryable": code == "UNAVAILABLE"}
    return status or HTTP_CODE[code], rec, headers or {}


class PublicApi:
    def __init__(self, orchestrator: Orchestrator, token: str, *, staff_token: Optional[str] = None, max_inflight: int = 32,
                 max_utterance_chars: int = 1000, alerts: Optional[AlertService] = None, max_subscriptions: int = 100,
                 retry_after_seconds: int = 1, state_poll_min_interval: float = 0.5) -> None:
        if not token:
            raise ValueError("공개 API 토큰이 비어 있다")
        if staff_token is not None and (not staff_token or hmac.compare_digest(staff_token.encode(), token.encode())):
            raise ValueError("직원 토큰은 비어 있지 않고 키오스크 토큰과 달라야 한다")
        self.orch, self.token, self.staff_token = orchestrator, token, staff_token
        self.alerts, self.max_subscriptions = alerts, max_subscriptions
        self.max_chars = max_utterance_chars
        self.retry_after = max(1, int(retry_after_seconds))
        self.state_poll_min_interval = state_poll_min_interval
        self._polls: dict[str, float] = {}
        self._poll_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._msg = jsonschema.Draft202012Validator(survey_schema.load_schema("FrontendMessage"))
        self._dec = jsonschema.Draft202012Validator(survey_schema.load_schema("InterventionDecision"))

    # ------------------------------------------------------------------ 진입
    def handle(self, method: str, path: str, auth: str, raw: bytes) -> tuple[int, dict, dict]:
        """(상태 코드, 본문, 추가 헤더). /healthz, /readyz는 service.py가 처리한다. 429·503에는 Retry-After(초, 정수)를 붙인다."""
        status, body, headers = self._handle(method, path, auth, raw)
        if status in (429, 503) and "Retry-After" not in headers:
            headers = {**headers, "Retry-After": str(self.retry_after)}
        return status, body, headers

    def _handle(self, method: str, path: str, auth: str, raw: bytes):
        path, _, query = path.partition("?")
        route = self._route(method, path)
        if route is None:
            return _err("NOT_FOUND", "경로가 없다")
        role = self._role(auth)
        if role is None:
            return _err("PERMISSION_DENIED", "토큰이 없거나 틀리다", 401, {"WWW-Authenticate": "Bearer"})
        if (route[0] in STAFF_ROUTES) != (role == "staff"):         # 직원 경로는 staff만, 키오스크 경로는 kiosk만
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
        if route[0] in STAFF_ROUTES - {"intervention"} and self.alerts is None:
            return _err("NOT_FOUND", "경로가 없다")
        if not self._slots.acquire(blocking=False):
            return _err("UNAVAILABLE", "요청이 너무 많다", 429)
        try:
            name, arg = route
            if name in ("alerts", "push_del"):
                return getattr(self, "_" + name)(parse_qs(query))
            if name in ("state", "push_config"):
                return getattr(self, "_" + name)(arg)
            return getattr(self, "_" + name)(body, arg)
        finally:
            self._slots.release()

    def _role(self, auth: str) -> Optional[str]:
        if not auth.startswith("Bearer "):
            return None
        tok = auth[7:].encode()
        kiosk = hmac.compare_digest(tok, self.token.encode())
        staff = self.staff_token is not None and hmac.compare_digest(tok, self.staff_token.encode())
        return "kiosk" if kiosk else ("staff" if staff else None)

    @staticmethod
    def _route(method: str, path: str):
        if method == "GET" and path == "/v1/staff/alerts":
            return ("alerts", None)
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
            out = self.orch.handle_turn(turn_id=turn_id, frontend_message=msg)
        except TurnIdConflict:
            return _err("CONFLICT", "같은 turn_id에 다른 입력이 왔다")
        return 200, out.frontend_input or {"response_text": None, "turn_failure_signal": None}, {}

    def _replayed(self, sid: str, turn_id: str) -> bool:
        try:
            return self.orch.store.get_turn(sid, turn_id) is not None
        except Exception:
            return False

    def _start(self, body: Mapping, _sid):
        rest, tid, err = self._envelope(body, required=False, reserved_ok=True)
        if err:
            return err
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

    def _intervention(self, body: Mapping, path_sid: str):
        rest, tid, err = self._envelope(body, required=True, reserved_ok=False)
        if err:
            return err
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
            out = self.orch.handle_turn(turn_id=tid, intervention_decision=dict(rest))
        except TurnIdConflict:
            return _err("CONFLICT", "같은 turn_id에 다른 입력이 왔다")
        self._resolve_if_applied(out, sid, rest["decision"])
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
    def _resolve_if_applied(self, out, sid: str, decision: Optional[str]) -> None:
        """개입 결정이 성공했으면 그 세션의 열린 알림을 해결한다(멱등: 같은 turn_id 재전송에서도 다시 해 본다)."""
        ir = out.orchestrator_output.get("intervention_result")
        if self.alerts is None or not ir or ir.get("result") != "APPLIED":
            return
        try:
            self.alerts.resolve(sid, decision)
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

    def _push_config(self, _arg):
        return 200, {"vapid_public_key": self.alerts.public_key()}, {}

    def _push_add(self, body: Mapping, _sid):
        ep, keys = body.get("endpoint"), body.get("keys")
        if not isinstance(ep, str) or not ep.startswith("https://") or len(ep) > 2048:
            return _err("INVALID_RECORD", "endpoint는 https URL이어야 한다")
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
