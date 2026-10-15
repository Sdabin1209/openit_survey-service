"""compose 스모크용 가짜 이웃: release-gate(I4)와 ops-feedback(I6)을 흉내 내는 참조 서버를 0.0.0.0:9000에 띄운다. 시험 대역이다."""
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .http_support import TOK, publish
from .ref_servers import ReferenceServer


def main(port: int = 9000) -> None:
    ref = ReferenceServer(TOK)
    publish(ref, Path(tempfile.mkdtemp(prefix="gate-")), "rel-compose")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            ref._serve(self)
        do_POST = do_GET

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    print("가짜 게이트/수집 서버 시작", port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
