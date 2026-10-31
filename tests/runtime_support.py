"""모델 추론 서버 시험 대역: 계약의 RuntimeReferenceServer(boundary-contracts의 conformance.servers)에 실패·지연·적재 지연을 꽂는 시험용 확장.

서버 동작(경로, 본문 검증, OUTPUT_INVALID 503)은 참조 서버 그대로이고, 이 클래스는 호출 전에 시험이 정한 사고를 일으킬 뿐이다."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from boundary_contracts import PIPELINE_SCHEMA_VERSION as V, BoundaryError

from conformance.servers import RuntimeReferenceServer


def question_id_of(req: dict) -> str:
    return req["question_id"]


def redirect_extraction(req: dict) -> dict:
    """어떤 입력에도 검증을 통과하는 A 출력: 같은 문항에 머무는 REDIRECT(OFF_TOPIC)."""
    return {"action": "REDIRECT", "next_item": question_id_of(req), "reason": "OFF_TOPIC", "values": []}


class FlakyRuntime(RuntimeReferenceServer):
    def __init__(self, tokens, rid="rel-1", *, load_polls: int = 0, load_error: BoundaryError | None = None, load_gate=None):
        super().__init__(tokens)
        self.available[rid] = "ok"
        self.rid = rid
        self.extract_fn = redirect_extraction
        self.respond_fn = lambda req: ("응답입니다.", "ok")
        self.put_fail_next: list = []      # PUT 호출마다 앞에서 하나씩: BoundaryError면 던진다
        self.fail_next: list = []          # 추론 POST마다 앞에서 하나씩: BoundaryError면 던지고, (상태, 유형, 바이트)면 그대로 응답한다
        self.puts = 0
        self.calls: list = []
        self.extract_bodies: list = []     # extract POST가 받은 본문(JSON). 서비스가 ExtractRequest를 보냈는지 본다
        self.load_polls, self.load_error = load_polls, load_error      # load_polls: 적재 상태를 이만큼 물어본 뒤에야 LOADED(시간이 아니라 질의 횟수라 부하와 무관하다)
        self._polls: dict = {}
        self.load_gate = load_gate         # threading.Event: 주면 적재는 시간이 아니라 이 이벤트가 켜질 때 끝난다(부하에서 '아직 LOADING'이 뒤집히지 않는다)
        self._load_started: dict = {}

    def start(self, port: int = 0) -> "FlakyRuntime":
        """참조 서버의 start와 같다. 정한 포트에 서버를 띄울 수 있게 포트를 받는다(서버가 늦게 뜨는 시험)."""
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer._serve(self)

            do_POST = do_PUT = do_GET

        self._server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def forget(self) -> None:
        """추론 컨테이너가 다시 떴다(적재 상태를 잊는다)."""
        self.loaded.clear(); self._loading.clear(); self._load_started.clear(); self._polls.clear()

    def _async_state(self, rid):
        t0 = self._load_started.get(rid)
        if t0 is None:
            return "ABSENT"
        if self.load_error is not None:
            return "FAILED"
        if self.load_gate is not None:
            return "LOADED" if self.load_gate.is_set() else "LOADING"
        n = self._polls[rid] = self._polls.get(rid, 0) + 1
        return "LOADED" if n > self.load_polls else "LOADING"

    def _route(self, method, path, q, body, role):
        parts = path.split("/")
        if len(parts) == 4 and parts[1:3] == ["v1", "releases"] and (self.load_polls or self.load_gate is not None or self.load_error):
            rid = parts[3]
            if method == "PUT":
                self.puts += 1
                if self.put_fail_next:
                    e = self.put_fail_next.pop(0)
                    if e is not None:
                        raise e
                self._load_started.setdefault(rid, True)
                state = self._async_state(rid)
                if state == "LOADED":
                    self.loaded.add(rid)
            elif method == "GET":
                state = self._async_state(rid)
            else:
                return super()._route(method, path, q, body, role)
            if state == "LOADED":
                self.loaded.add(rid)
            err = self.load_error.to_record(V) if state == "FAILED" else None
            return self._j({"release_id": rid, "state": state, "error": err}, 202 if state == "LOADING" and method == "PUT" else 200)
        if len(parts) == 4 and method == "PUT":
            self.puts += 1
            if self.put_fail_next:
                e = self.put_fail_next.pop(0)
                if e is not None:
                    raise e
        if len(parts) == 5 and method == "POST":
            self.calls.append(parts[4])
            if parts[4] == "extract":
                self.extract_bodies.append(json.loads(body))
            if self.fail_next:
                step = self.fail_next.pop(0)
                if isinstance(step, BoundaryError):
                    raise step
                if step is not None:
                    return step
            if (self.load_polls or self.load_gate is not None) and parts[3] not in self.loaded:
                raise BoundaryError("NOT_FOUND", "적재되지 않은 릴리스")
        return super()._route(method, path, q, body, role)


def raw_response(status: int, body, ctype: str = "application/json", headers: dict | None = None):
    data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    return (status, ctype, data, headers or {})
