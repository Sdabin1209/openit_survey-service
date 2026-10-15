"""추론 서버: 모델 런타임(ModelSession)을 HTTP 뒤에 둔다. 서빙 서버의 추론 컨테이너로 분리할 때 이 모듈을 띄운다.

릴리스 적재는 서비스의 로더와 같은 검사(승인, 해시, 계약·라이브러리 버전)를 거친다. 지금 구현된 백엔드는 fake뿐이다.
설정: SURVEY_INFERENCE_PORT(8081), SURVEY_INFERENCE_TOKEN_ENV(토큰이 든 환경 변수의 이름, 기본 SURVEY_INFERENCE_TOKEN),
SURVEY_MODEL_BACKEND, SURVEY_RELEASE_READER(+_URL, _TOKEN_ENV), SURVEY_WORK_DIR.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import model_runtime
import turn_engine

from .inference import InferenceServer
from .release import ReleaseLoader, ReleaseRegistry
from .release.loader import LoadRejected
from .settings import ConfigError, Settings, load_factory

log = logging.getLogger("survey_service.inference_server")
STATUS = {"INVALID_RECORD": 400, "PERMISSION_DENIED": 403, "NOT_FOUND": 404, "FAILED_PRECONDITION": 412,
          "UNAVAILABLE": 503, "INTEGRITY_ERROR": 422}


class InferenceHttp:
    def __init__(self, loader: ReleaseLoader, registry: ReleaseRegistry, token: str) -> None:
        self.loader, self.registry, self.token = loader, registry, token
        self._lock = threading.Lock()

    @staticmethod
    def _err(code: str, message: str):
        return STATUS[code], {"code": code, "message": message, "retryable": code == "UNAVAILABLE"}

    def handle(self, method: str, path: str, auth: str, body: bytes):
        if method == "GET" and path in ("/healthz", "/readyz"):
            return 200, {"ok": True}
        if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.token)):
            return self._err("PERMISSION_DENIED", "토큰이 없거나 틀리다")
        m = re.fullmatch(r"/v1/releases/([^/]+)(|/parse|/generate)", path)
        if not m:
            return self._err("NOT_FOUND", "경로가 없다")
        rid, op = m.group(1), m.group(2)
        if method == "PUT" and not op:
            with self._lock:                                   # 같은 릴리스를 동시에 두 번 적재하지 않는다
                if rid not in self.registry.loaded_ids():
                    missing = self.loader.ensure_loaded([rid])
                    if missing:
                        return self._err("FAILED_PRECONDITION", "릴리스를 적재할 수 없다")
            return 200, {"release_id": rid, "loaded": True}
        if method == "POST" and op:
            try:
                rel = self.registry.get(rid)
            except Exception:
                return self._err("NOT_FOUND", "적재되지 않은 릴리스")
            try:
                inp = json.loads(body)
            except ValueError:
                return self._err("INVALID_RECORD", "JSON이 아니다")
            try:
                out = rel.session.parse(inp) if op == "/parse" else rel.session.generate(inp)
            except model_runtime.ContractViolation:
                return self._err("INVALID_RECORD", "입력이 계약을 어긴다")
            except Exception as e:                              # 인프라 문제만 예외다(출력 문제는 출력 보호가 처리)
                log.error("추론 실패 release=%s op=%s error=%s", rid, op, type(e).__name__)
                return self._err("UNAVAILABLE", "추론에 실패했다")
            return 200, out
        return self._err("NOT_FOUND", "경로가 없다")


def make_server(app: InferenceHttp, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def _do(self):
            n = int(self.headers.get("Content-Length") or 0)
            status, rec = app.handle(self.command, self.path, self.headers.get("Authorization", ""), self.rfile.read(n) if n else b"")
            raw = json.dumps(rec, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        do_GET = do_POST = do_PUT = _do
    return ThreadingHTTPServer(("0.0.0.0", port), H)


def build(settings: Settings, reader=None, token: Optional[str] = None) -> InferenceHttp:
    reg = ReleaseRegistry()
    if reader is None:
        spec = "survey_service.clients:http_release_reader" if settings.release_reader == "http" else settings.release_reader
        reader = load_factory(spec)()
    loader = ReleaseLoader(reader, reg, InferenceServer(settings.model_backend), workdir=__import__("pathlib").Path(settings.work_dir),
                           installed_versions={"turn_engine_version": turn_engine.ENGINE_VERSION,
                                               "model_runtime_version": model_runtime.RUNTIME_VERSION})
    if token is None:
        name = os.environ.get("SURVEY_INFERENCE_TOKEN_ENV", "SURVEY_INFERENCE_TOKEN")
        token = os.environ.get(name, "")
        if not token:
            raise ConfigError(f"추론 서버 토큰이 환경 변수 {name}에 없다")
    return InferenceHttp(loader, reg, token)


def main() -> int:
    from .service import configure_logging
    try:
        s = Settings.from_env({**os.environ, "SURVEY_TRACE_INGEST": os.environ.get("SURVEY_TRACE_INGEST", "unused:unused")})
        app = build(s)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    configure_logging(s.log_level)
    port = int(os.environ.get("SURVEY_INFERENCE_PORT", "8081"))
    httpd = make_server(app, port)
    log.info("추론 서버 시작 port=%s backend=%s", port, s.model_backend)
    import signal
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
