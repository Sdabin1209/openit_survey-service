"""서비스 조립과 HTTP(/healthz, /readyz만). 공개 API(I10)는 O06 대기라 만들지 않는다."""
from __future__ import annotations

import json
import logging
import os
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
from boundary_contracts import PIPELINE_SCHEMA_VERSION

from . import SERVICE_VERSION
from .api import PublicApi
from .inference import InferenceServer
from .remote import RemoteInference
from .orchestrator import Orchestrator
from .release import ReleaseLoader, ReleaseRegistry
from .settings import ConfigError, Settings, load_factory
from .store.sqlite import SqliteSessionStore
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


class App:
    """저장소·릴리스 로더·Trace 내보내기·Orchestrator를 조립한다. reader/ingest를 주면 팩토리 설정을 쓰지 않는다(시험)."""

    def __init__(self, settings: Settings, *, reader=None, ingest=None, staff_console=None, dispatcher=None) -> None:
        self.settings = settings
        Path(settings.work_dir).mkdir(parents=True, exist_ok=True)
        self.store = SqliteSessionStore(settings.session_db)
        self.outbox = Outbox(settings.outbox_db)
        self.registry = ReleaseRegistry()
        if settings.inference_url:                        # 모델 런타임을 HTTP 경계 뒤에(추론 컨테이너)
            tok = os.environ.get(settings.inference_token_env, "")
            if not tok:
                raise ConfigError(f"추론 서버 토큰이 환경 변수 {settings.inference_token_env}에 없다")
            self.inference = RemoteInference(settings.inference_url, tok, settings.inference_timeout_seconds)
        else:
            self.inference = InferenceServer(settings.model_backend)
        self.reader = reader if reader is not None else load_factory(_spec(settings.release_reader, 'release'))()
        self.ingest = ingest if ingest is not None else load_factory(_spec(settings.trace_ingest, 'trace'))()
        self.loader = ReleaseLoader(
            self.reader, self.registry, self.inference, workdir=Path(settings.work_dir), keep_releases=settings.keep_releases,
            installed_versions={"turn_engine_version": turn_engine.ENGINE_VERSION, "model_runtime_version": model_runtime.RUNTIME_VERSION},
            open_release_ids=self.store.open_release_ids)
        self.exporter = TraceExporter(self.outbox, self.ingest, base_delay=settings.export_interval_seconds,
                                      max_delay=settings.export_backoff_max_seconds)
        console = staff_console if staff_console is not None else (load_factory(settings.staff_console)() if settings.staff_console else None)
        disp = dispatcher if dispatcher is not None else (load_factory(settings.result_dispatcher)() if settings.result_dispatcher else None)
        self.orchestrator = Orchestrator(self.store, self.registry, staff_console=console, dispatcher=disp,
                                         trace_sink=OutboxTraceSink(self.outbox))
        api_token = os.environ.get(settings.api_token_env, "")
        if not api_token:
            raise ConfigError(f"공개 API 토큰이 환경 변수 {settings.api_token_env}에 없다")
        staff_token = os.environ.get(settings.staff_api_token_env, "")
        if not staff_token:
            raise ConfigError(f"직원 개입 토큰이 환경 변수 {settings.staff_api_token_env}에 없다")
        if staff_token == api_token:
            raise ConfigError("직원 개입 토큰은 키오스크 토큰과 달라야 한다")
        self.api = PublicApi(self.orchestrator, api_token, staff_token=staff_token, max_inflight=settings.api_max_inflight,
                             max_utterance_chars=settings.api_max_utterance_chars)
        self._stop = threading.Event()
        self._threads: list = []

    # ------------------------------------------------------------------ 수명
    def start(self) -> None:
        self.loader.poll_once()                                   # 첫 활성 릴리스를 가능하면 바로 적재한다
        self.loader.ensure_loaded(self.store.open_release_ids())  # 다시 띄운 뒤 진행 중 세션의 릴리스(턴 밖에서)
        self.exporter.start(self.settings.export_interval_seconds)

        def poll():
            n = 0
            while not self._stop.wait(self.settings.release_poll_seconds):
                try:
                    self.loader.poll_once()
                    if self.loader.status.unavailable_releases:
                        self.loader.ensure_loaded(self.store.open_release_ids())
                    n += 1
                    if n % 60 == 0:
                        self.outbox.purge_sent(time.time() - self.settings.outbox_keep_sent_hours * 3600)
                except Exception as e:
                    log.error("릴리스 폴링 실패 error=%s", type(e).__name__)
        t = threading.Thread(target=poll, name="release-poller", daemon=True)
        t.start()
        self._threads.append(t)
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

    # ------------------------------------------------------------------ 상태
    def readiness(self) -> tuple[bool, dict]:
        problems = []
        try:
            self.store.ping()
        except Exception:
            problems.append("session_db")
        try:
            self.outbox.ping()
        except Exception:
            problems.append("outbox_db")
        active = self.registry.active_id()
        if active is None:
            problems.append("no_active_release")
        body = {
            "ready": not problems, "problems": problems, "service_version": SERVICE_VERSION,
            "active_release": active, "contract_version": survey_schema.current_version(),
            "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
            "api": {"idle_expiry_seconds": self.settings.session_idle_seconds},
            "model_backend": self.inference.backend, "model_backend_production_safe": self.inference.production_safe,
            "release_loader": {"unavailable_releases": self.loader.status.unavailable_releases,
                               "last_pointer_version": self.loader.status.last_pointer_version,
                               "last_poll_error": self.loader.status.last_poll_error,
                               "rejections": self.loader.status.rejections[-5:], "warnings": self.loader.status.warnings[-5:]},
            "trace_outbox": self.exporter.stats(),
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
            code, body, hdr = app.api.handle(self.command, self.path, self.headers.get("Authorization", ""), raw)
            out = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out)))
            for k, v in hdr.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            if self.path == "/healthz":
                self._send(200, {"status": "ok"})
            elif self.path == "/readyz":
                ok, body = app.readiness()
                self._send(200 if ok else 503, body)
            else:
                self._send(404, {"error": "not found"})

        def _other(self):
            self._send(404, {"error": "not found"})            # 공개 API 밖의 경로·메서드는 없다
        do_POST = _api
        do_PUT = do_DELETE = do_PATCH = _other
    return Handler


def serve(app: App, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    try:
        app = App(settings)
    except Exception as e:
        log.error("시작 실패 error=%s message=%s", type(e).__name__, e if isinstance(e, ConfigError) else "")
        return 3
    app.start()
    httpd = serve(app, settings.port)
    log.info("시작 port=%s version=%s", settings.port, SERVICE_VERSION)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        app.stop()
    return 0
