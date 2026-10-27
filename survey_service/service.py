"""서비스 조립과 HTTP(/healthz, /readyz만). 공개 API(I10)는 O06 대기라 만들지 않는다."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
import os
import queue
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import model_runtime
import turn_engine
import survey_schema
from boundary_contracts import PIPELINE_SCHEMA_VERSION, SecretError, read_secret, secret_source

from . import SERVICE_VERSION
from .alerts import AlertHub, AlertService, AlertStaffConsole, AlertStore, WebPushSender
from .api import PublicApi, _err
from .inference import InferenceServer
from .remote import RemoteInference
from .orchestrator import Orchestrator
from .release import ReleaseLoader, ReleaseRegistry
from .risk_rules import RuleRiskSource
from .errors import StoreError
from .settings import ConfigError, Settings, load_factory
from .db import open_stores
from .trace import Outbox, OutboxTraceSink, TraceExporter

log = logging.getLogger("survey_service")


class JsonFormatter(logging.Formatter):
    """로그는 표준 출력에 JSON 한 줄. 발화·모델 출력·Trace 내용은 어디서도 로그로 내지 않는다."""
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z", "level": record.levelname,
                           "logger": record.name, "msg": record.getMessage()}, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


def _spec(spec: str, which: str) -> str:
    """'http'는 boundary_contracts의 HTTP 클라이언트 기본 팩토리(survey_service.clients)."""
    if spec == "http":
        return "survey_service.clients:http_release_reader" if which == "release" else "survey_service.clients:http_trace_ingest"
    return spec


def _secret(name: str, what: str):
    """비밀 하나를 NAME_FILE 우선, 없으면 NAME 환경 변수에서 읽는다(D84). 오류 메시지에 값은 없다."""
    try:
        return read_secret(name)
    except SecretError as e:
        raise ConfigError(f"{what}: {e}") from None


class App:
    """저장소·릴리스 로더·Trace 내보내기·Orchestrator를 조립한다. reader/ingest를 주면 팩토리 설정을 쓰지 않는다(시험)."""

    def __init__(self, settings: Settings, *, reader=None, ingest=None, staff_console=None, dispatcher=None, risk_source=None) -> None:
        self.settings = settings
        Path(settings.work_dir).mkdir(parents=True, exist_ok=True)
        self.store, self.outbox, self.alert_store, self.db_kind = open_stores(settings)
        self.outbox.max_pending = settings.outbox_max_pending
        push = None
        vapid_key = _secret(settings.vapid_private_key_env, "VAPID 비밀키") or ""
        if vapid_key:                                       # 키가 없으면 푸시 없이 SSE만 동작한다
            if not settings.vapid_subject:
                raise ConfigError("VAPID 비밀키가 있는데 SURVEY_VAPID_SUBJECT(mailto: 또는 https://)가 없다")
            try:
                push = WebPushSender(vapid_key, settings.vapid_subject, settings.push_timeout_seconds, allow_private_hosts=settings.push_allow_private_hosts)
            except ValueError as e:
                raise ConfigError(str(e)) from None
        self.alerts = AlertService(self.alert_store, AlertHub(), push, settings.push_timeout_seconds)
        self.registry = ReleaseRegistry()
        if settings.inference_url:                        # 모델 런타임을 HTTP 경계 뒤에(추론 컨테이너)
            tok = _secret(settings.inference_token_env, "추론 서버 토큰")
            if not tok:
                raise ConfigError(f"추론 서버 토큰이 {settings.inference_token_env}_FILE(파일) 또는 환경 변수 {settings.inference_token_env}에 없다")
            self.inference = RemoteInference(settings.inference_url, tok, settings.inference_timeout_seconds,
                                         load_timeout=settings.inference_load_timeout_seconds, retries=settings.inference_retries)
        else:
            self.inference = InferenceServer(settings.model_backend)
        self.reader = reader if reader is not None else load_factory(_spec(settings.release_reader, 'release'))()
        self.ingest = ingest if ingest is not None else load_factory(_spec(settings.trace_ingest, 'trace'))()
        self.loader = ReleaseLoader(
            self.reader, self.registry, self.inference, workdir=Path(settings.work_dir), keep_releases=settings.keep_releases,
            installed_versions={"turn_engine_version": turn_engine.ENGINE_VERSION, "model_runtime_version": model_runtime.RUNTIME_VERSION},
            open_release_ids=self.store.open_release_ids, retry_base_seconds=settings.release_retry_base_seconds,
            retry_max_seconds=settings.release_retry_max_seconds, give_up_seconds=settings.release_retry_give_up_seconds)
        self.exporter = TraceExporter(self.outbox, self.ingest, base_delay=settings.export_interval_seconds,
                                      max_delay=settings.export_backoff_max_seconds)
        console = staff_console if staff_console is not None else (load_factory(settings.staff_console)() if settings.staff_console else None)
        disp = dispatcher if dispatcher is not None else (load_factory(settings.result_dispatcher)() if settings.result_dispatcher else None)
        if console is None:                                   # 기본 직원 접점은 알림 어댑터. SURVEY_STAFF_CONSOLE로 바꿔 꽂을 수 있다
            console = AlertStaffConsole(self.alerts)
        if settings.risk_rules_file and settings.risk_source:
            raise ConfigError("SURVEY_RISK_RULES_FILE과 SURVEY_RISK_SOURCE는 함께 쓸 수 없다(입력원은 하나)")
        if risk_source is None and settings.risk_rules_file:
            risk_source = RuleRiskSource.from_file(settings.risk_rules_file)        # 오류는 ConfigError: 시작을 거부한다
            d = risk_source.describe()
            log.info("위험 규칙 적재 version=%s rules=%s sha256=%s", d["version"], d["rule_count"], d["sha256"])
        elif risk_source is None and settings.risk_source:
            risk_source = load_factory(settings.risk_source)()
        self.orchestrator = Orchestrator(self.store, self.registry, risk_source=risk_source, staff_console=console, dispatcher=disp,
                                         trace_sink=OutboxTraceSink(self.outbox), timeline=self.alert_store,
                                         max_consecutive_reask=settings.max_consecutive_reask,
                                         turn_deadline_seconds=settings.turn_deadline_seconds)
        api_token = _secret(settings.api_token_env, "공개 API 토큰")
        if not api_token:
            raise ConfigError(f"공개 API 토큰이 {settings.api_token_env}_FILE(파일) 또는 환경 변수 {settings.api_token_env}에 없다")
        staff_token = _secret(settings.staff_api_token_env, "직원 개입 토큰")
        if not staff_token:
            raise ConfigError(f"직원 개입 토큰이 {settings.staff_api_token_env}_FILE(파일) 또는 환경 변수 {settings.staff_api_token_env}에 없다")
        if staff_token == api_token:
            raise ConfigError("직원 개입 토큰은 키오스크 토큰과 달라야 한다")
        result_token = _secret(settings.result_reader_token_env, "결과 조회 토큰") or None     # 없으면 해당 API가 꺼진다
        risk_token = _secret(settings.risk_source_token_env, "위험 신호 토큰") or None
        if result_token in (api_token, staff_token) or risk_token in (api_token, staff_token) or (result_token and result_token == risk_token):
            raise ConfigError("토큰은 역할마다 서로 달라야 한다")
        self.api = PublicApi(self.orchestrator, api_token, staff_token=staff_token, alerts=self.alerts, max_subscriptions=settings.max_push_subscriptions,
                             result_token=result_token, risk_token=risk_token, require_subject_ref=settings.require_subject_ref, push_allow_private=settings.push_allow_private_hosts,
                             retry_after_seconds=settings.retry_after_seconds, state_poll_min_interval=settings.state_poll_min_interval_seconds,
                             max_inflight=settings.api_max_inflight,
                             max_utterance_chars=settings.api_max_utterance_chars)
        self._stop = threading.Event()
        self.draining = threading.Event()        # SIGTERM을 받았다: /readyz는 503(shutting_down), 처리 중 요청은 끝낸다
        self._threads: list = []

    # ------------------------------------------------------------------ 수명
    def start(self) -> None:
        self.exporter.start(self.settings.export_interval_seconds)
        first_done = threading.Event()

        def poll():
            n = 0
            first = True
            # 첫 폴링은 바로 한다(첫 활성 릴리스, 다시 띄운 뒤 진행 중 세션의 릴리스). 추론 서버가 아직 뜨는 중이면 오래 걸릴 수 있어
            # 이 스레드에서 하고, start()는 startup_wait_seconds까지만 기다린다(그동안에도 /healthz는 산다).
            while first or not self._stop.wait(self.settings.release_poll_seconds):
                try:
                    self.loader.poll_once()
                    if first:
                        self.loader.ensure_loaded(self.store.open_release_ids())
                    elif self.loader.status.unavailable_releases:
                        self.loader.ensure_loaded(self.store.open_release_ids())
                    n += 1
                    if n % 60 == 0:
                        self.outbox.purge_sent(time.time() - self.settings.outbox_keep_sent_hours * 3600)
                        self.alert_store.trim_events(keep=10000)      # SSE 이어 받기 범위(최근 1만 건) 밖의 이벤트를 지운다. 알림 행은 남는다
                except Exception as e:
                    log.error("릴리스 폴링 실패 error=%s", type(e).__name__)
                if first:
                    first = False
                    first_done.set()
        t = threading.Thread(target=poll, name="release-poller", daemon=True)
        t.start()
        self._threads.append(t)
        first_done.wait(self.settings.startup_wait_seconds)
        if self.settings.session_idle_seconds > 0:
            idle = self.settings.session_idle_seconds

            def sweep():
                while not self._stop.wait(max(1.0, min(30.0, idle / 4))):
                    try:
                        self.api.expire_idle(self.store.list_idle(time.time() - idle))
                    except Exception as e:
                        log.error("유휴 만료 주기 실패 error=%s", type(e).__name__)
            t2 = threading.Thread(target=sweep, name="idle-sweeper", daemon=True)
            t2.start()
            self._threads.append(t2)

    def stop(self) -> None:
        self._stop.set()
        self.exporter.stop()
        for t in self._threads:
            t.join(timeout=5)
        self.store.close()
        self.outbox.close()
        self.alert_store.close()

    # ------------------------------------------------------------------ 상태
    def _secret_sources(self) -> dict:
        """비밀 출처만(file|env|unset). 값·파일 경로·변수 이름은 내지 않는다(D84)."""
        st = self.settings
        names = {"api_token": st.api_token_env, "staff_api_token": st.staff_api_token_env, "vapid_private_key": st.vapid_private_key_env,
                 "result_reader_token": st.result_reader_token_env, "risk_source_token": st.risk_source_token_env}
        if st.inference_url:
            names["inference_token"] = st.inference_token_env
        if st.release_reader == "http":
            names["release_reader_token"] = st.release_reader_token_env
        if st.trace_ingest == "http":
            names["trace_ingest_token"] = st.trace_ingest_token_env
        return {k: secret_source(v) for k, v in names.items()}

    def _bounded(self, fns: dict, budget: float) -> dict:
        """함수들을 데몬 스레드에서 동시에 돌리고 `budget`초 안에 끝난 것만 {이름: ("ok", 값) | ("error", None)}로 돌려준다(못 끝낸 것은 빠진다).
        /readyz가 DB 호출 하나 때문에 오래 붙잡히지 않게 하는 마지막 상한이다."""
        out: dict = {}

        def run(name, fn):
            try:
                out[name] = ("ok", fn())
            except Exception:
                out[name] = ("error", None)
        ts = [threading.Thread(target=run, args=(n, f), name="readyz-" + n, daemon=True) for n, f in fns.items()]
        for t in ts:
            t.start()
        end = time.monotonic() + budget
        for t in ts:
            t.join(max(0.0, end - time.monotonic()))
        return dict(out)

    def _db_problems(self) -> list:
        """세 저장소를 **동시에** 확인한다(직렬이면 연결 불가 때 시간이 저장소 수만큼 곱해진다). PostgreSQL의 각 확인은 주 연결·락을
        쓰지 않는 별도 짧은 연결이고 자체 상한(`readyz_db_timeout_seconds`)이 있다. 그래도 안 끝나면 여유 1초 뒤 실패로 센다."""
        res = self._bounded({"session_db": self.store.ping, "outbox_db": self.outbox.ping, "alert_db": self.alert_store.ping},
                            self.settings.readyz_db_timeout_seconds + 1.0)
        return [n for n in ("session_db", "outbox_db", "alert_db") if res.get(n, ("error",))[0] != "ok"]

    def _db_stats(self, db_ok: bool) -> dict:
        """DB를 읽는 보조 수치(outbox 건수, 구독 수). 주 연결을 쓰므로 DB가 정상일 때만 읽고, 1초 안에 못 읽거나 DB가 비정상이면
        마지막으로 읽은 값에 `stale=true`를 붙여 돌려준다. 이 읽기가 실패해도 /readyz는 응답한다(예외가 핸들러를 끊지 않게)."""
        if db_ok:
            res = self._bounded({"stats": lambda: {"trace_outbox": self.exporter.stats(), "staff_alerts": self.alerts.status()}}, 1.0)
            if res.get("stats", ("error",))[0] == "ok":
                self._db_stats_cache = res["stats"][1]
                return dict(self._db_stats_cache, stale=False)
        c = getattr(self, "_db_stats_cache", None)
        return dict(c, stale=True) if c else {"trace_outbox": None, "staff_alerts": None, "stale": True}

    def readiness(self) -> tuple[bool, dict]:
        problems = self._db_problems()
        if self.draining.is_set():
            problems.append("shutting_down")
        dbs = self._db_stats(not problems)
        active = self.registry.active_id()
        retrying = self.loader.retry_info()
        if active is None:
            problems.append("no_active_release")
            if retrying is not None:
                problems.append("release_loading")      # 거부가 아니라 일시적 실패로 재시도 중이다(release_loader.retrying)
        body = {
            "ready": not problems, "problems": problems, "database": ({"kind": self.db_kind} if not any(p.endswith("_db") for p in problems) else
                         {"kind": self.db_kind, "status": "UNAVAILABLE", "detail": "db unavailable"}), "service_version": SERVICE_VERSION,
            "active_release": active, "contract_version": survey_schema.current_version(),
            "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
            "api": {"idle_expiry_seconds": self.settings.session_idle_seconds},
            "staff_alerts": dbs["staff_alerts"],
            "secrets": self._secret_sources(),
            # D74: v2의 A 출력에는 위험 신호 필드가 없어 직원 알림의 입력원이 없다. 조용히 끄지 않고 여기에 드러낸다(CR-012, 헤드 결정 대기).
            "staff_alert_source": ((self.orchestrator.risk_source.describe() if hasattr(self.orchestrator.risk_source, "describe")
                                    else {"active": True}) if self.orchestrator.risk_source is not None else
                                   {"active": False, "reason": "NO_INPUT_SOURCE_IN_V2_EXTRACTION",
                                    "detail": "ActionExtraction에는 위험 신호·직원 호출 필드가 없다. 알림 저장·전달 경로는 살아 있으나 알림이 만들어지지 않는다(docs/change-requests/CR-012)."}),
            "validation_violations": self.orchestrator.violation_stats(),
            "result_dispatcher": self.orchestrator.dispatcher_status(),
            "reask_limit": {"active": self.settings.max_consecutive_reask > 0, "max_consecutive": self.settings.max_consecutive_reask},
            "response_tables": {"status": self.orchestrator.tables.status, "counters": dict(self.orchestrator.tables.stats)},
            "question_flow": {"flow_id": self.orchestrator.flow.flow_id, "items": len(self.orchestrator.flow.table.order)},
            "inference": (self.inference.stats.snapshot() if hasattr(self.inference, "stats") else None),
            "model_backend": self.inference.backend, "model_backend_production_safe": self.inference.production_safe,
            "release_loader": {"retrying": retrying, "unavailable_releases": self.loader.status.unavailable_releases,
                               "last_pointer_version": self.loader.status.last_pointer_version,
                               "last_poll_error": self.loader.status.last_poll_error,
                               "rejections": self.loader.status.rejections[-5:], "warnings": self.loader.status.warnings[-5:]},
            "trace_outbox": dbs["trace_outbox"], "db_stats_stale": dbs["stale"],
        }
        return not problems, body


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "survey-service"

        def log_message(self, *a):                                # 접근 로그에 경로 외의 것을 남기지 않는다
            return

        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _api(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if 0 < n <= 1_000_000 else b""
            code, body, hdr = app.api.handle(self.command, self.path, self.headers.get("Authorization", ""), raw,
                                             user=(self.headers.get("X-Openit-User") or "").strip() or None)
            out = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out)))
            for k, v in hdr.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(out)

        def _stream(self):
            """GET /v1/staff/alerts/stream: SSE. Last-Event-ID가 있으면 그 뒤 이벤트부터 이어 받는다."""
            err = app.api.stream_auth_error(self.headers.get("Authorization", ""))
            if err:
                code, body, hdr = err
                out = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(out)))
                for k, v in hdr.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(out)
                return
            hub, store = app.alerts.hub, app.alert_store
            last = (self.headers.get("Last-Event-ID") or "").strip()
            q = hub.subscribe()                               # 먼저 구독하고 나서 저장된 이벤트를 읽는다(빠지는 구간이 없게)
            try:
                try:
                    latest, oldest = store.latest_event_id(), store.oldest_event_id()
                except StoreError:                             # 알림 DB가 죽어 있으면 200 스트림을 열지 않고 503(클라이언트는 Retry-After 뒤 Last-Event-ID로 다시 연결)
                    code, body, hdr = _err("UNAVAILABLE", "알림 저장소를 읽을 수 없다", 503, {"Retry-After": str(app.settings.retry_after_seconds)})
                    out = json.dumps(body, ensure_ascii=False).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(out)))
                    for k, v in hdr.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(out)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                self._heartbeat()                                  # 연결 직후에도 한 번(콘솔이 '연결됨'을 확인할 수 있게)
                sent = -1
                if last.isdigit():                               # id는 이벤트 순번. 끊긴 동안의 이벤트를 이어서 보낸다
                    sent = int(last)
                    if sent > latest or (oldest is not None and sent < oldest - 1):      # 이어 받을 수 없다(오래된 이벤트가 지워졌거나 순번이 앞섰다): 목록 API로 다시 맞추게 알린다
                        self.wfile.write(b'event: resync\ndata: {"reason":"EVENTS_NOT_RESUMABLE"}\n\n')
                        self.wfile.flush()
                    for ev in store.events_after(sent):
                        self._sse(ev)
                        sent = ev["id"]
                last_ping = time.monotonic()
                while not app._stop.is_set() and not app.draining.is_set() and not getattr(q, "overflowed", False):
                    try:
                        ev = q.get(timeout=min(app.settings.sse_heartbeat_seconds, 0.5))      # 종료 신호를 0.5초 안에 본다(우아한 종료가 스트림을 기다리지 않게)
                    except queue.Empty:
                        if time.monotonic() - last_ping >= app.settings.sse_heartbeat_seconds:
                            self._heartbeat()
                            last_ping = time.monotonic()
                        continue
                    if ev["id"] > sent:                          # 이어 받기 구간과 겹치는 이벤트는 건너뛴다
                        self._sse(ev)
                        sent = ev["id"]
            except OSError:                                    # 클라이언트가 끊었다
                pass
            except StoreError:                                 # 알림 DB 오류: 스트림을 끝낸다(클라이언트는 Last-Event-ID로 다시 연결한다)
                log.warning("알림 스트림을 DB 오류로 끝낸다")
            finally:
                hub.unsubscribe(q)

        def _heartbeat(self) -> None:
            """주석 줄(`: ping`, 프록시·연결 유지용)과 이름 있는 이벤트 `heartbeat`(브라우저 EventSource가 앱 코드에서 볼 수 있다, id 없음 = Last-Event-ID를 바꾸지 않는다).
            DB를 읽지 않는다: 알림 DB·상류가 느리거나 죽어도 스트림 스레드는 하트비트를 계속 보낸다. 기존 클라이언트는 모르는 이벤트 이름을 무시한다(CR-016)."""
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.wfile.write(b": ping\n\nevent: heartbeat\ndata: " + json.dumps({"ts": ts}).encode() + b"\n\n")
            self.wfile.flush()

        def _sse(self, ev: dict) -> None:
            self.wfile.write(f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(ev['alert'], ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/v1/staff/alerts/stream":
                self._stream()
            elif self.path.startswith("/v1/"):
                self._api()
            elif self.path == "/healthz":
                self._send(200, {"status": "ok"})
            elif self.path == "/readyz":
                ok, body = app.readiness()
                self._send(200 if ok else 503, body)
            else:
                self._send(404, {"error": "not found"})

        def _other(self):
            self._send(404, {"error": "not found"})            # 공개 API 밖의 경로·메서드는 없다
        do_POST = do_DELETE = _api
        do_PUT = do_PATCH = _other
    return Handler


class _Server(ThreadingHTTPServer):
    # 우아한 종료: 요청 스레드는 데몬이 아니고 server_close()가 처리 중인 요청이 끝나기를 기다린다(턴은 총 마감 안에 끝난다).
    daemon_threads = False
    block_on_close = True
    # socketserver 기본 대기열(request_queue_size)은 5라, 동시에 접속이 몰리면 연결이 거부·재설정된다(부하 측정 N=50에서 확인). 일반적인 값으로 올린다.
    request_queue_size = 128


def serve(app: App, port: int) -> ThreadingHTTPServer:
    return _Server(("0.0.0.0", port), make_handler(app))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["purge"]:
        from .retention import run_purge
        return run_purge()
    if argv:
        print("알 수 없는 명령: 서버는 인자 없이, 보존 정리는 `purge`", file=sys.stderr)
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
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, on_term)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        app.stop()
    return 0
