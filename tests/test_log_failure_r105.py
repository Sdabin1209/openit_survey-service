"""RUNTIME R10.5: 로그 쓰기 실패(표준 출력이 막힘·닫힘)로 요청을 실패시키거나 느리게 하지 않는다. 줄에는 요청 ID와 예외 요약이 그대로 남는다."""
import io
import json
import logging
import threading

from survey_service.wiring import app as app_mod
from survey_service.wiring.app import _REQ, configure_logging

from .test_service import get, running, settings  # noqa: F401,F811


class BrokenStream(io.TextIOBase):
    def write(self, s):
        raise OSError(5, "I/O error")

    def flush(self):
        raise OSError(5, "I/O error")


class BlockedStream(io.TextIOBase):
    """쓰기가 풀릴 때까지 막히는 표준 출력(파이프가 가득 참)."""

    def __init__(self):
        self.gate, self.lines = threading.Event(), []

    def write(self, s):
        self.gate.wait(30)
        self.lines.append(s)
        return len(s)

    def flush(self):
        pass


def _restore():
    configure_logging("INFO")


def test_requests_succeed_when_the_log_stream_raises(running):
    app, reader, ingest, port = running
    configure_logging("INFO", BrokenStream())
    try:
        log = logging.getLogger("survey_service.test")
        for _ in range(5):
            log.warning("줄")
            log.error("예외", exc_info=ValueError("x"))
        st, body = get(port, "/healthz")
        assert st == 200 and body
        assert get(port, "/readyz")[0] in (200, 503)
    finally:
        _restore()


def test_a_blocked_log_stream_does_not_block_the_request_thread(running):
    app, reader, ingest, port = running
    stream = BlockedStream()
    configure_logging("INFO", stream)
    try:
        lg = logging.getLogger("survey_service.test")
        done = threading.Event()

        def work():
            for _ in range(200):
                lg.warning("막힌 출력에도 이 호출은 즉시 끝난다")
            done.set()
        t = threading.Thread(target=work, daemon=True); t.start()
        assert done.wait(10)                                  # 쓰기 스레드가 막혀 있어도 호출한 스레드는 끝났다
        st, _ = get(port, "/healthz")
        assert st == 200
    finally:
        stream.gate.set()
        _restore()


def test_queued_lines_keep_the_request_id_and_exception_summary():
    buf = io.StringIO()
    configure_logging("INFO", buf)
    try:
        _REQ.rid = "req-abcdef12"
        try:
            raise ValueError("비밀 메시지 S3CRET")
        except ValueError:
            logging.getLogger("survey_service.test").exception("처리 중 예기치 않은 예외")
        app_mod._listener.stop()                                # 쓰기 스레드가 큐를 비우게 한다
        rec = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert rec["request_id"] == "req-abcdef12" and rec["exc"]["types"] == ["builtins.ValueError"] and "S3CRET" not in buf.getvalue()
    finally:
        _REQ.rid = None
        app_mod._listener = None
        _restore()
