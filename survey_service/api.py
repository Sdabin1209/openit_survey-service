"""공개 API(I10): 키오스크 프론트 <-> 문진 서비스 (boundary-contracts/PUBLIC_API.md, D55).

POST /v1/sessions                    세션 시작   본문 {session_id, kind: SESSION_START}
POST /v1/sessions/{id}/turns         환자 발화   본문 {session_id, kind: UTTERANCE, utterance, turn_id}  (turn_id 멱등)
POST /v1/sessions/{id}/end           세션 종료   본문 {session_id, kind: SESSION_END}
응답은 FrontendInput. 오류는 BoundaryError 본문과 상태 코드. 문서가 정하지 않은 점의 해석은 docs/change-requests/CR-009.

이 모듈은 HTTP를 모른다(handle이 상태 코드와 본문을 돌려준다). 연결은 service.py가 한다.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from typing import Mapping, Optional

import jsonschema

import survey_schema
from boundary_contracts import PIPELINE_SCHEMA_VERSION

from .errors import TurnIdConflict
from .orchestrator import Orchestrator

log = logging.getLogger("survey_service.api")

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TURN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_BODY = 64 * 1024
HTTP_CODE = {"INVALID_RECORD": 400, "PERMISSION_DENIED": 401, "NOT_FOUND": 404, "CONFLICT": 409, "UNAVAILABLE": 503}
START_TURN_ID, END_TURN_ID, EXPIRE_TURN_ID = "start", "end", "idle-expire"   # 호출마다 turn_id가 없는 동작의 고정 ID(CR-009)


def _err(code: str, message: str, status: Optional[int] = None, headers: Optional[dict] = None):
    rec = {"pipeline_schema_version": PIPELINE_SCHEMA_VERSION, "code": code, "message": message,
           "retryable": code == "UNAVAILABLE"}
    return status or HTTP_CODE[code], rec, headers or {}


class PublicApi:
    def __init__(self, orchestrator: Orchestrator, token: str, *, max_inflight: int = 32, max_utterance_chars: int = 1000) -> None:
        if not token:
            raise ValueError("공개 API 토큰이 비어 있다")
        self.orch, self.token = orchestrator, token
        self.max_chars = max_utterance_chars
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._msg = jsonschema.Draft202012Validator(survey_schema.load_schema("FrontendMessage"))
        self._fi = jsonschema.Draft202012Validator(survey_schema.load_schema("FrontendInput"))

    # ------------------------------------------------------------------ 진입
    def handle(self, method: str, path: str, auth: str, raw: bytes) -> tuple[int, dict, dict]:
        """(상태 코드, 본문, 추가 헤더). /healthz, /readyz는 service.py가 처리한다."""
        route = self._route(method, path)
        if route is None:
            return _err("NOT_FOUND", "경로가 없다")
        if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), self.token.encode())):
            return _err("PERMISSION_DENIED", "토큰이 없거나 틀리다", 401, {"WWW-Authenticate": "Bearer"})
        if len(raw) > MAX_BODY:
            return _err("INVALID_RECORD", "본문이 너무 크다")
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            return _err("INVALID_RECORD", "본문이 JSON이 아니다")
        if not isinstance(body, dict):
            return _err("INVALID_RECORD", "본문이 JSON 객체가 아니다")
        if not self._slots.acquire(blocking=False):
            return _err("INVALID_RECORD", "요청이 너무 많다", 429, {"Retry-After": "1"})
        try:
            return getattr(self, "_" + route[0])(body, route[1])
        finally:
            self._slots.release()

    @staticmethod
    def _route(method: str, path: str):
        path = path.split("?", 1)[0]
        if method == "POST" and path == "/v1/sessions":
            return ("start", None)
        m = re.fullmatch(r"/v1/sessions/([^/]+)/(turns|end)", path)
        if method == "POST" and m:
            return ("turn" if m.group(2) == "turns" else "end", m.group(1))
        return None

    # ------------------------------------------------------------------ 검증
    def _message(self, body: Mapping, path_sid: Optional[str], kind: str, extra: tuple = ()):
        """FrontendMessage를 만든다. 문제가 있으면 (None, 오류 응답)."""
        allowed = {"session_id", "kind", *extra} | ({"utterance"} if kind == "UTTERANCE" else set())
        unknown = set(body) - allowed
        if unknown:
            return None, _err("INVALID_RECORD", f"알 수 없는 필드: {sorted(unknown)}")
        sid = body.get("session_id")
        if not isinstance(sid, str) or not SESSION_ID.match(sid):
            return None, _err("INVALID_RECORD", "session_id는 영문·숫자·-_ 1~64자여야 한다")
        if path_sid is not None and path_sid != sid:
            return None, _err("INVALID_RECORD", "경로의 세션 ID와 본문의 session_id가 다르다")
        if body.get("kind") != kind:
            return None, _err("INVALID_RECORD", f"kind는 {kind}여야 한다")
        msg = {k: body[k] for k in ("session_id", "kind", "utterance") if k in body}
        if kind == "UTTERANCE":
            u = msg.get("utterance")
            if not isinstance(u, str) or not u.strip():
                return None, _err("INVALID_RECORD", "utterance가 비었다")
            if len(u) > self.max_chars:
                return None, _err("INVALID_RECORD", f"utterance가 {self.max_chars}자를 넘는다")
        if list(self._msg.iter_errors(msg)):
            return None, _err("INVALID_RECORD", "FrontendMessage 형식이 아니다")
        return msg, None

    # ------------------------------------------------------------------ 동작
    def _state(self, sid: str):
        r = self.orch.store.load(sid)
        if r["status"] == "DB_ERROR":
            return "ERROR", None
        return (r["state"]["progress_record"]["session_status"] if r["status"] == "OK" else None), r

    def _run(self, sid: str, turn_id: str, msg: Mapping):
        try:
            out = self.orch.handle_turn(turn_id=turn_id, frontend_message=msg)
        except TurnIdConflict:
            return _err("CONFLICT", "같은 turn_id에 다른 입력이 왔다")
        fi = out.frontend_input
        if fi is None:                                 # 프론트가 보낸 말에는 항상 FrontendInput이 있다(처리 안 함은 null/null)
            fi = {"response_text": None, "turn_failure_signal": None}
        return 200, fi, {}

    def _start(self, body: Mapping, _sid):
        msg, err = self._message(body, None, "SESSION_START")
        if err:
            return err
        sid = msg["session_id"]
        if self._replayed(sid, START_TURN_ID):
            return self._run(sid, START_TURN_ID, msg)
        status, _ = self._state(sid)
        if status == "ERROR" or (status is None and self.orch.releases.active() is None):
            return _err("UNAVAILABLE", "지금은 세션을 시작할 수 없다")
        if status and status.startswith("ENDED"):
            return _err("CONFLICT", "이미 끝난 세션이다. 새 세션 ID로 시작해야 한다")
        return self._run(sid, START_TURN_ID, msg)

    def _turn(self, body: Mapping, path_sid: str):
        msg, err = self._message(body, path_sid, "UTTERANCE", extra=("turn_id",))
        if err:
            return err
        turn_id = body.get("turn_id")
        if not isinstance(turn_id, str) or not TURN_ID.match(turn_id):
            return _err("INVALID_RECORD", "turn_id는 영문·숫자 등 1~128자 문자열이어야 한다")
        sid = msg["session_id"]
        if turn_id in (START_TURN_ID, END_TURN_ID, EXPIRE_TURN_ID):
            return _err("INVALID_RECORD", f"turn_id {turn_id}는 서버가 쓰는 예약 값이다")
        if self._replayed(sid, turn_id):
            return self._run(sid, turn_id, msg)
        status, _ = self._state(sid)
        if status == "ERROR":
            return _err("UNAVAILABLE", "지금은 처리할 수 없다")
        if status is None:
            return _err("NOT_FOUND", "모르는 세션이다. 먼저 세션을 시작해야 한다")
        if status.startswith("ENDED"):
            return _err("CONFLICT", "이미 끝난 세션이다")
        return self._run(sid, turn_id, msg)

    def _end(self, body: Mapping, path_sid: str):
        msg, err = self._message(body, path_sid, "SESSION_END")
        if err:
            return err
        sid = msg["session_id"]
        if not self._replayed(sid, END_TURN_ID):
            status, _ = self._state(sid)
            if status == "ERROR":
                return _err("UNAVAILABLE", "지금은 처리할 수 없다")
            if status is None:
                return _err("NOT_FOUND", "모르는 세션이다")
            if status.startswith("ENDED"):                 # 이미 끝난 세션의 종료는 같은 결과(말할 것 없음)
                return 200, {"response_text": None, "turn_failure_signal": None}, {}
        return self._run(sid, END_TURN_ID, msg)

    def _replayed(self, sid: str, turn_id: str) -> bool:
        try:
            return self.orch.store.get_turn(sid, turn_id) is not None
        except Exception:
            return False

    # ------------------------------------------------------------------ 유휴 만료
    def expire_idle(self, session_ids) -> int:
        """유휴 세션을 사용자 이탈(SESSION_END)로 닫는다. 결과 전달이 불린다. 닫은 개수."""
        n = 0
        for sid in session_ids:
            try:
                out = self.orch.handle_turn(turn_id=EXPIRE_TURN_ID, frontend_message={"session_id": sid, "kind": "SESSION_END"})
                n += 0 if out.replayed else 1
            except Exception as e:
                log.error("유휴 세션을 닫지 못했다 error=%s", type(e).__name__)
        return n
