"""서비스 조립과 HTTP 서버(/healthz, /readyz, 공개 API 연결, 직원 알림 SSE). 공개 API 라우팅은 api.py(D55), 설정은 settings.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
import math
import queue
import signal
import sys
import threading
import time
import uuid
from typing import Optional

from http import HTTPStatus

from boundary_contracts import PIPELINE_SCHEMA_VERSION
from openit_link import Request, Response
from waitress import create_server
from waitress import wasyncore

from . import SERVICE_VERSION
from .components.link_public_api import _err
from .settings import ConfigError, Settings
from .wiring.errors import StoreError

log = logging.getLogger("survey_service")


from .wiring.app import App, _REQ, _REQ_ID, configure_logging
_WAITRESS_INTERNAL_FDS = 2     # waitress는 connection_limit에 듣기 소켓과 깨우기(trigger) 소켓을 함께 센다: 측정(limit=4일 때 동시 연결 2개만 응답)으로 확인, 그만큼 더해야 설정이 클라이언트 연결 수가 된다 (ADR-0029)
_BODY_LIMIT = 1_000_000          # 요청 본문 상한(바이트). waitress max_request_body_size로 적용되고 넘으면 waitress가 413을 낸다 (ADR-0029)


def _status_line(code: int) -> str:
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return f"{code} Unknown"


class _SseBody:
    """SSE 응답 본문(WSGI iterable). waitress는 클라이언트가 끊으면 다음 쓰기에서 반복을 멈추고 close()를 부른다: 구독을 거기서 푼다."""

    def __init__(self, app: App, q, sent: int, hub, allowed=None) -> None:
        self._app, self._q, self._sent, self._hub, self._closed = app, q, sent, hub, False
        self._allowed = allowed          # 이 연결이 받을 이벤트 이름 집합(X-Openit-Roles로 거른 것, 최소 권한). None이면 모두

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._hub.unsubscribe(self._q)

    def __iter__(self):
        app, q, sent = self._app, self._q, self._sent
        try:
            last_ping = started = time.monotonic()
            max_life = app.settings.sse_max_seconds
            # 최대 수명(R2.4): 인증 만료·철회 신호를 스트림이 받지 못하므로, 수명이 다하면 서버가 정상 종료한다(클라이언트가 새로 연결하고 인증이 끝났으면 그 연결이 401)
            while not app._stop.is_set() and not app.draining.is_set() and not getattr(q, "overflowed", False) and time.monotonic() - started < max_life:
                try:
                    ev = q.get(timeout=min(app.settings.sse_heartbeat_seconds, 0.5))      # 종료 신호를 0.5초 안에 본다(우아한 종료가 스트림을 기다리지 않게)
                except queue.Empty:
                    if time.monotonic() - last_ping >= app.settings.sse_heartbeat_seconds:
                        yield _heartbeat()
                        last_ping = time.monotonic()
                    continue
                if self._allowed is not None and ev["type"] not in self._allowed:
                    if ev["id"] is not None and ev["id"] > sent:
                        sent = ev["id"]                      # 거른 이벤트도 순번은 지나간 것으로 센다(이어 받기 때 다시 보내지 않게)
                    continue
                if ev["id"] is None:                         # result_ready: 저장·순번이 없는 실시간 이벤트(U83, ADR-0056). Last-Event-ID를 바꾸지 않는다
                    yield _sse(ev)
                elif ev["id"] > sent:                        # 이어 받기 구간과 겹치는 이벤트는 건너뛴다
                    yield _sse(ev)
                    sent = ev["id"]
        except StoreError:                                 # 알림 DB 오류: 스트림을 끝낸다(클라이언트는 Last-Event-ID로 다시 연결한다)
            log.warning("알림 스트림을 DB 오류로 끝낸다")
        finally:
            self.close()


_ROLE_EVENTS = {"staff": {"alert", "resolved"}, "clinician": {"result_ready"}}      # 역할이 받는 이벤트(최소 권한). heartbeat·resync는 모두에게


def _checked(name: str, data: dict) -> dict:
    """내보내기 전 생산자 검증(CONVENTIONS 3절): 이벤트 data를 계약 스키마로 본다. 어기면 ValueError(스트림을 끝낸다)."""
    errs = list(_validator("StaffStreamEvent").iter_errors({"event": name, "data": data}))
    if errs:
        raise ValueError("SSE event violates StaffStreamEvent")
    return data


_validators: dict = {}


def _validator(name: str):
    from boundary_contracts import validator
    v = _validators.get(name)
    if v is None:
        v = _validators[name] = validator(name)
    return v


def _allowed_events(headers) -> Optional[set]:
    """X-Openit-Roles로 연결이 받을 이벤트 이름을 정한다. 헤더가 없으면(직접 호출·옛 경로) staff 몫. 알려진 역할이 하나도 없으면 heartbeat·resync만."""
    raw = headers.get("x-openit-roles")
    roles = {"staff"} if raw is None else {r.strip() for r in raw.split(",")}
    allowed = {"heartbeat", "resync"}
    for r in roles:
        allowed |= _ROLE_EVENTS.get(r, set())
    return allowed


def _heartbeat() -> bytes:
    """주석 줄(`: ping`, 프록시·연결 유지용)과 이름 있는 이벤트 `heartbeat`(브라우저 EventSource가 앱 코드에서 볼 수 있다, id 없음 = Last-Event-ID를 바꾸지 않는다).
    DB를 읽지 않는다: 알림 DB·상류가 느리거나 죽어도 스트림은 하트비트를 계속 보낸다. 기존 클라이언트는 모르는 이벤트 이름을 무시한다(CR-016)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return b": ping\n\nevent: heartbeat\ndata: " + json.dumps(_checked("heartbeat", {"ts": ts})).encode() + b"\n\n"


def _sse(ev: dict) -> bytes:
    _checked(ev["type"], ev["alert"])
    if ev["id"] is None:
        return f"event: {ev['type']}\ndata: {json.dumps(ev['alert'], ensure_ascii=False)}\n\n".encode()
    return f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(ev['alert'], ensure_ascii=False)}\n\n".encode()


def _stream(app: App, req: Request) -> Response:
    """GET /v1/staff/alerts/stream: SSE. Last-Event-ID가 있으면 그 뒤 이벤트부터 이어 받는다. 인증(staff 역할)은 라이브러리가 이미 했다."""
    hub, store = app.alerts.hub, app.alert_store
    retry = {"Retry-After": str(app.settings.retry_after_seconds)}
    if hub.count() >= app.settings.max_sse_connections:      # 스트림은 연결마다 작업 스레드를 쥔다: 상한을 넘으면 503
        raise _err("UNAVAILABLE", "알림 스트림 연결이 너무 많다", 429, retry, details={"reason": "SSE_CONNECTION_LIMIT"})      # 동시 연결 상한 초과는 429(PUBLIC_API): 클라이언트는 Retry-After 뒤 다시 연결한다
    last = req.headers.get("last-event-id", "").strip()
    q = hub.subscribe()                               # 먼저 구독하고 나서 저장된 이벤트를 읽는다(빠지는 구간이 없게)
    try:
        latest, oldest = store.latest_event_id(), store.oldest_event_id()
        first = [b"retry: 3000\n\n", _heartbeat()]           # 연결 직후에도 한 번(콘솔이 '연결됨'을 확인할 수 있게)
        sent = -1
        if last.isdigit():                               # id는 이벤트 순번. 끊긴 동안의 이벤트를 이어서 보낸다
            sent = int(last)
            if sent > latest or (oldest is not None and sent < oldest - 1):      # 이어 받을 수 없다(오래된 이벤트가 지워졌거나 순번이 앞섰다): 목록 API로 다시 맞추게 알린다
                first.append(b"event: resync\ndata: " + json.dumps(_checked("resync", {"reason": "EVENTS_NOT_RESUMABLE"})).encode() + b"\n\n")
            allowed = _allowed_events(req.headers)
            for ev in store.events_after(sent):
                if ev["type"] in allowed:
                    first.append(_sse(ev))
                sent = ev["id"]
    except StoreError:                             # 알림 DB가 죽어 있으면 200 스트림을 열지 않고 503(클라이언트는 Retry-After 뒤 Last-Event-ID로 다시 연결한다)
        hub.unsubscribe(q)
        raise _err("UNAVAILABLE", "알림 저장소를 읽을 수 없다", 503, retry) from None
    return Response(200, _chain(first, _SseBody(app, q, sent, hub, _allowed_events(req.headers))), {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    content_type="text/event-stream; charset=utf-8")


def make_wsgi(app: App):
    """WSGI(PEP 3333) 애플리케이션. 서버(소켓, 스레드, 접속·시간 제한)는 waitress가 맡고, /v1/·/healthz·/readyz는 openit_link.App(공개 API 컴포넌트의 경로 목록)이 받는다 (ADR-0029).
    여기에 남기는 것: X-Request-Id와 로그 필드, 204에 본문 없음, 공개 API 밖 경로의 404."""
    link = app.api.link_app(ready=app.readiness, stream=lambda req: _stream(app, req))

    def application(environ, start_response):
        method = environ["REQUEST_METHOD"]
        raw_path = environ.get("RAW_URI") or environ.get("REQUEST_URI") or environ.get("PATH_INFO", "/")
        path = raw_path.split("?", 1)[0]
        rid = environ.get("HTTP_X_REQUEST_ID", "").strip()
        _REQ.rid = rid if _REQ_ID.match(rid) else uuid.uuid4().hex[:16]

        def respond(code: int, body: dict, extra: Optional[dict] = None):
            raw = json.dumps(body, ensure_ascii=False).encode()
            return finish(code, {"Content-Type": "application/json", **(extra or {})}, raw)

        def finish(code: int, headers: dict, body):
            out = [("X-Request-Id", _REQ.rid)]
            if code == 204:      # 왜: 204는 본문이 없어야 한다(RFC 9110 15.3.5). 본문과 Content-Type을 보내지 않는다
                start_response(_status_line(204), out + [(k, v) for k, v in headers.items() if k not in ("Content-Type", "Content-Length")])
                return [b""]
            headers = dict(headers)
            if headers.get("Content-Type") == "application/json":
                headers["Content-Type"] = "application/json; charset=utf-8"
            if isinstance(body, (bytes, bytearray)):
                headers["Content-Length"] = str(len(body))
                body = [bytes(body)]
            start_response(_status_line(code), out + list(headers.items()))      # 반복자(SSE)는 길이를 모른다: 나눠 보낸다
            return body

        if path.startswith("/v1/"):
            try:
                if int(environ.get("CONTENT_LENGTH") or 0) < 0:
                    raise ValueError
            except ValueError:                                     # waitress가 먼저 걸러 보통 오지 않는다. 오면 400
                return respond(400, {"pipeline_schema_version": PIPELINE_SCHEMA_VERSION, "code": "INVALID_RECORD",
                                     "message": "Content-Length가 올바르지 않다", "retryable": False})
            return finish(*_order(app.api.dispatch(environ, link)))
        if path in ("/healthz", "/readyz"):
            return finish(*_order(link.handle(environ)))
        if path == "/metrics" and method == "GET":      # 내부망 전용(Prometheus가 직접 긁는다). deploy-exposure.json에서 denied라 gateway로 나가지 않는다 (ADR-0055)
            body, ctype = app.metrics.render()
            return finish(200, {"Content-Type": ctype, "Cache-Control": "no-store"}, body)
        return respond(404, {"error": "not found"})            # 공개 API 밖의 경로·메서드는 없다

    return application


def _order(result: tuple) -> tuple:
    """라이브러리는 (상태, 머리글, 본문)을 낸다. finish의 인자 순서와 같다."""
    return result


class _chain:
    """앞에 보낼 조각들을 먼저 내고 본문 iterable을 잇는다. close()는 본문에 전달한다."""

    def __init__(self, first, body) -> None:
        self._first, self._body = first, body

    def __iter__(self):
        yield from self._first
        yield from self._body

    def close(self) -> None:
        self._body.close()


def serve(app: App, port: int):
    """waitress 서버(단일 프로세스·다중 스레드)를 만든다. `.run()`이 블록하고 `close_server()`로 닫는다.
    값의 출처는 docs/adr/ADR-0029.md: 작업 스레드는 처리 중 요청 상한 + SSE 상한 + 여유(스트림이 요청 하나가 끝날 때까지 스레드를 쥐므로),
    연결 상한·읽기 시간 제한은 기존 설정을 그대로(측정 안 함), backlog는 이전 값 128(부하 측정 N=50 근거)."""
    st = app.settings
    return create_server(
        make_wsgi(app), host=st.host, port=port, ident="survey-service", threads=st.api_max_inflight + st.max_sse_connections + 4,
        connection_limit=st.max_connections + _WAITRESS_INTERNAL_FDS, channel_timeout=max(1, math.ceil(st.socket_timeout_seconds)), cleanup_interval=1,
        max_request_body_size=_BODY_LIMIT, backlog=128, expose_tracebacks=False)


def close_server(server, wait_seconds: float) -> None:
    """우아한 종료: 새 접속을 받지 않고, 처리 중인 요청이 끝나기를 기다린 뒤(최대 wait_seconds) 남은 연결을 닫는다. 호출 전에 app.draining을 세워 SSE가 끝나게 한다."""
    if getattr(server, "_survey_closed", False):
        return
    server._survey_closed = True
    server.close()
    dispatcher = server.task_dispatcher
    end = time.monotonic() + wait_seconds
    while dispatcher.active_count and time.monotonic() < end:
        time.sleep(0.05)
    dispatcher.shutdown(cancel_pending=True, timeout=1)
    wasyncore.close_all(server._map)


def _close_logged(server, wait_seconds: float) -> None:
    """SIGTERM 처리 스레드의 일꾼: close_server의 예외를 기록하고 삼킨다(스레드 예외로 남기지 않는다, S6). 닫기에 실패해도 주 스레드의 finally가 다시 닫는다."""
    try:
        close_server(server, wait_seconds)
    except Exception as e:
        log.error("우아한 종료 중 오류 error=%s", type(e).__name__)


def run_server(server) -> None:
    """서버 루프를 돈다. close_server가 다른 스레드에서 소켓을 닫으면 waitress의 select가 EBADF(OSError)로 깨어난다: 닫기를 요청한 뒤라면 정상 종료이므로 삼키고,
    닫기를 요청하지 않았는데 난 OSError는 그대로 올린다. 왜: 정상 종료(SIGTERM)가 추적 로그와 종료 코드 1로 끝나던 결함(smoke 진단 출력으로 발견, S5)."""
    try:
        server.run()
    except OSError:
        if not getattr(server, "_survey_closed", False):
            raise


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["purge"]:
        from .wiring.purge_job import run_purge
        return run_purge(argv=argv[1:])
    if argv[:1] == ["backup"]:
        from .components.link_backup import main as backup_main
        return backup_main()
    if argv[:1] == ["restore"]:
        from .components.link_backup import restore_main
        return restore_main(argv[1:])
    if argv:
        print("알 수 없는 명령: 서버는 인자 없이, 보존 정리는 `purge`, 백업은 `backup`", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    try:
        app = App(settings)
    except Exception as e:
        log.error("시작 실패 error=%s message=%s", type(e).__name__, e if isinstance(e, (ConfigError, StoreError)) else "")
        return 3
    app.start()
    httpd = serve(app, settings.port)
    log.info("시작 port=%s version=%s", settings.port, SERVICE_VERSION)
    def on_term(*_):
        app.draining.set()                      # /readyz가 먼저 503이 되어 로드 밸런서가 새 요청을 보내지 않게 한다
        threading.Thread(target=_close_logged, args=(httpd, settings.turn_deadline_seconds + 1.0), daemon=True).start()
    signal.signal(signal.SIGTERM, on_term)
    try:
        run_server(httpd)
    except KeyboardInterrupt:
        pass
    finally:
        close_server(httpd, 0.0)
        app.stop()
    return 0
