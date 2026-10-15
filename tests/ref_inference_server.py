"""추론 서버 참조 구현(시험 대역). 서비스 이미지에 들어가지 않는다.

docs/inference-server-spec.md의 규약을 model-runtime의 가짜 경로(FakeModelSession, 모델 없음)로 구현한다. 실제 서버(vLLM 컨테이너)는
model-runtime 영역이 만든다. 이 참조 서버는 (1) 서비스 쪽 클라이언트의 시험 상대, (2) compose 스모크의 가짜 추론 컨테이너,
(3) 규약의 실행 가능한 예다. 시험용 결함 주입: delay(응답 지연), fail_next(다음 호출의 응답을 통째로 바꿈), load_delay(적재 지연, 비동기).
설정(환경 변수, main): INFERENCE_PORT(8081), INFERENCE_TOKEN(토큰 값), SURVEY_RELEASE_READER=http + _URL + _TOKEN_ENV, SURVEY_MODEL_BACKEND=fake.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import model_runtime
import turn_engine

from survey_service.inference import InferenceServer
from survey_service.release import ReleaseLoader, ReleaseRegistry
from survey_service.settings import ConfigError, Settings, load_factory

log = logging.getLogger("ref_inference_server")
STATUS = {"INVALID_RECORD": 400, "PERMISSION_DENIED": 403, "NOT_FOUND": 404, "FAILED_PRECONDITION": 412,
          "UNAVAILABLE": 503, "INTEGRITY_ERROR": 422, "UNSUPPORTED_VERSION": 422}


def err(code: str, message: str, **details):
    rec = {"code": code, "message": message, "retryable": code == "UNAVAILABLE"}
    if details:
        rec["details"] = details
    return STATUS[code], rec, {}


class InferenceHttp:
    def __init__(self, loader: ReleaseLoader, registry: ReleaseRegistry, token: str, *, load_delay: float = 0.0,
                 async_load: bool = False) -> None:
        self.loader, self.registry, self.token = loader, registry, token
        self.load_delay, self.async_load = load_delay, async_load
        self.delay = 0.0                       # parse/generate 응답 지연(초)
        self.fail_next: list = []              # 다음 parse/generate 호출의 (상태, 본문 dict|bytes, 헤더)
        self.calls: list = []                  # (op, release_id)
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}      # rid -> {"state", "error"}

    # ---- 적재 ----
    def _load(self, rid: str) -> None:
        if self.load_delay:
            time.sleep(self.load_delay)
        missing = self.loader.ensure_loaded([rid])
        if missing:
            rej = self.loader.status.rejections
            self._state[rid] = {"state": "FAILED", "error": {"code": "FAILED_PRECONDITION", "message": "릴리스를 적재할 수 없다",
                                                              "retryable": False}}
        else:
            self._state[rid] = {"state": "LOADED", "error": None}

    def handle(self, method: str, path: str, auth: str, body: bytes):
        if method == "GET" and path in ("/healthz", "/readyz"):
            return 200, {"ok": True}, {}
        if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.token)):
            return err("PERMISSION_DENIED", "토큰이 없거나 틀리다")
        if method == "GET" and path == "/v1/info":
            return 200, {"service": "ref-inference-server", "runtime_version": model_runtime.RUNTIME_VERSION,
                         "supported_contract_versions": [1], "backend": "fake", "loaded": self.registry.loaded_ids()}, {}
        m = re.fullmatch(r"/v1/releases/([^/]+)(|/parse|/generate)", path)
        if not m:
            return err("NOT_FOUND", "경로가 없다")
        rid, op = m.group(1), m.group(2)
        if method == "GET" and not op:
            st = self._state.get(rid) or ({"state": "LOADED", "error": None} if rid in self.registry.loaded_ids()
                                          else {"state": "ABSENT", "error": None})
            return 200, {"release_id": rid, **st}, {}
        if method == "PUT" and not op:
            with self._lock:
                if rid in self.registry.loaded_ids():
                    return 200, {"release_id": rid, "state": "LOADED", "error": None}, {}
                cur = self._state.get(rid)
                if cur and cur["state"] == "LOADING":
                    return 202, {"release_id": rid, "state": "LOADING", "error": None}, {}
                self._state[rid] = {"state": "LOADING", "error": None}
                if self.async_load:
                    threading.Thread(target=self._load, args=(rid,), daemon=True).start()
                    return 202, {"release_id": rid, "state": "LOADING", "error": None}, {}
            self._load(rid)
            st = self._state[rid]
            if st["state"] == "FAILED":
                s, rec, h = err("FAILED_PRECONDITION", "릴리스를 적재할 수 없다")
                return s, rec, h
            return 200, {"release_id": rid, **st}, {}
        if method == "POST" and op:
            self.calls.append((op[1:], rid))
            if self.fail_next:
                return self.fail_next.pop(0)
            if self.delay:
                time.sleep(self.delay)
            try:
                rel = self.registry.get(rid)
            except Exception:
                return err("NOT_FOUND", "적재되지 않은 릴리스")
            try:
                inp = json.loads(body)
            except ValueError:
                return err("INVALID_RECORD", "JSON이 아니다")
            try:
                out = rel.session.parse(inp) if op == "/parse" else rel.session.generate(inp)
            except model_runtime.ContractViolation:
                return err("INVALID_RECORD", "입력이 계약을 어긴다")
            except Exception as e:                              # 인프라 문제만 예외다(출력 문제는 출력 보호가 처리)
                log.error("추론 실패 release=%s op=%s error=%s", rid, op, type(e).__name__)
                return err("UNAVAILABLE", "추론에 실패했다")
            info = getattr(rel.session, "last_call_info", None)
            hdr = {"X-Openit-Outcome": getattr(info, "outcome", "ok"), "X-Openit-Attempts": str(getattr(info, "attempts", 1))}
            return 200, out, hdr
        return err("NOT_FOUND", "경로가 없다")


def make_server(app: InferenceHttp, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            return

        def _do(self):
            n = int(self.headers.get("Content-Length") or 0)
            status, rec, hdr = app.handle(self.command, self.path, self.headers.get("Authorization", ""), self.rfile.read(n) if n else b"")
            raw = rec if isinstance(rec, bytes) else json.dumps(rec, ensure_ascii=False).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in hdr.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)
            except OSError:                                       # 클라이언트가 먼저 끊었다(타임아웃)
                pass
        do_GET = do_POST = do_PUT = _do
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    return srv


def build(settings: Settings, reader=None, token: Optional[str] = None, inference: Optional[InferenceServer] = None, **kw) -> InferenceHttp:
    reg = ReleaseRegistry()
    if reader is None:
        spec = "survey_service.clients:http_release_reader" if settings.release_reader == "http" else settings.release_reader
        reader = load_factory(spec)()
    loader = ReleaseLoader(reader, reg, inference or InferenceServer(settings.model_backend), workdir=Path(settings.work_dir),
                           installed_versions={"turn_engine_version": turn_engine.ENGINE_VERSION,
                                               "model_runtime_version": model_runtime.RUNTIME_VERSION})
    if token is None:
        token = os.environ.get("INFERENCE_TOKEN", "")
        if not token:
            raise ConfigError("INFERENCE_TOKEN이 없다")
    return InferenceHttp(loader, reg, token, **kw)


def main() -> int:
    from survey_service.service import configure_logging
    try:
        s = Settings.from_env({**os.environ, "SURVEY_TRACE_INGEST": os.environ.get("SURVEY_TRACE_INGEST", "unused:unused")})
        app = build(s)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    configure_logging(s.log_level)
    port = int(os.environ.get("INFERENCE_PORT", "8081"))
    httpd = make_server(app, port)
    log.info("참조 추론 서버(가짜 경로) 시작 port=%s", port)
    import signal
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
