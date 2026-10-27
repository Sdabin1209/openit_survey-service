"""compose 스모크용 가짜 모델 추론 서버: 계약의 RuntimeReferenceServer(HTTP.md I9 v2)를 0.0.0.0에 띄운다. 모델은 없다(시험 대역).

LoRA-A는 같은 문항에 머무는 REDIRECT(OFF_TOPIC)를 내고 LoRA-B는 고정 문장 "(가짜 응답)"을 낸다. 어떤 릴리스 ID든 적재 신호를 받는다."""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from boundary_contracts import read_secret

from .ref_servers import RuntimeReferenceServer
from .runtime_support import redirect_extraction


class _Any(dict):
    def get(self, k, d=None):
        return "ok"


def main() -> None:
    srv = RuntimeReferenceServer({read_secret("INFERENCE_TOKEN", required=True): "survey-service"})   # INFERENCE_TOKEN_FILE 우선(D84)
    srv.available = _Any()
    srv.extract_fn = redirect_extraction
    srv.respond_fn = lambda req: ("(가짜 응답)", "ok")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            srv._serve(self)
        do_POST = do_PUT = do_GET

    port = int(os.environ.get("INFERENCE_PORT", "8081"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), H)
    httpd.daemon_threads = True
    print("가짜 추론 서버 시작", port, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
