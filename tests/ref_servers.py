# boundary-contracts conformance/servers.py의 복사본(시험 전용). 패키지에 포함되지 않아 경로로 읽어 복사했다.
# 바꾼 곳은 _example()의 예시 경로뿐이다(환경 변수 BOUNDARY_CONTRACTS_EXAMPLES 또는 이웃 읽기 전용 리포의 examples/).
"""HTTP.md를 따르는 작은 참조 서버(표준 라이브러리 http.server). 적합성 시험용이며 서비스 구현이 아니다.

레코드는 메모리 딕셔너리에 넣어 둔다. 서버는 넣어 둔 내용을 검증하지 않고 그대로 낸다(일부러 틀린 내용을 넣어
클라이언트의 검증을 시험하려는 것이다). 토큰은 {토큰: 역할}이며 역할 "reader"는 읽기 전용이다.
`overrides[경로] = (상태, 콘텐츠 타입, 바이트[, 헤더 딕셔너리])`로 응답을 통째로 바꾸고, `delay`로 응답을 늦춘다.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from boundary_contracts import PIPELINE_SCHEMA_VERSION as V, BoundaryError, content_hash, sha256_hash

STATUS = {"INVALID_RECORD": 400, "PERMISSION_DENIED": 403, "NOT_FOUND": 404, "CONFLICT": 409,
          "FAILED_PRECONDITION": 412, "UNSUPPORTED_VERSION": 422, "INTEGRITY_ERROR": 422, "UNAVAILABLE": 503}
NOW = "2026-10-15T00:00:00Z"


def _example(name: str) -> dict:
    import os
    base = os.environ.get("BOUNDARY_CONTRACTS_EXAMPLES") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "boundary-contracts", "examples")
    with open(os.path.join(base, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def _submission_view(x: dict) -> dict:
    return {"submission_id": x["submission_id"], "submitted_at": x["submitted_at"], "submitted_by_role": x["submitted_by_role"],
            "subject": x["subject"], "status": "AWAITING_APPROVAL", "decision_id": None, "approval_id": None,
            "release_id": None, "updated_at": x["submitted_at"]}


def adapter_digest(files: dict[str, bytes]) -> str:
    """canonical.directory_hash와 같은 값(메모리 파일 버전)."""
    return sha256_hash([{"path": p, "sha256": "sha256:" + hashlib.sha256(files[p]).hexdigest()} for p in sorted(files)])


def make_tar(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def ref_of(id_: str, version: str, obj) -> dict:
    return {"id": id_, "version": version, "content_hash": sha256_hash(obj)}


def sealed(rec: dict) -> dict:
    rec = dict(rec)
    rec["content_hash"] = content_hash(rec)
    return rec


class ReferenceServer:
    def __init__(self, tokens: dict[str, str]):
        self.tokens = tokens
        self.overrides: dict[str, tuple] = {}
        self.delay = 0.0
        self.question_sets: dict[str, list[dict]] = {}   # id -> [{"bank", "ref", "reviews", "change"}]
        self.packages: dict[tuple, dict] = {}            # (id, version) -> {"manifest", "files", "report"}
        self.eval_reports: dict[str, bytes] = {}
        self.releases: dict[str, dict] = {}              # id -> {"manifest","bank","adapters":{role:{path:bytes}},"pass":bool}
        self.release_order: list[str] = []               # 오래된 것부터
        self.decisions: list[dict] = []                  # 오래된 것부터
        self.pointer: dict | None = None
        self.submissions: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.traces: dict[tuple, dict] = {}
        self.cases: list[dict] = []
        self.deid_traces: dict[str, dict] = {}
        self.requests: list[tuple] = []                  # (method, path+query, role)
        self._server = None

    # ---- 데이터 넣기 ----
    def add_question_set(self, id_: str, bank: dict, reviews=(), change=None) -> dict:
        versions = self.question_sets.setdefault(id_, [])
        ref = ref_of(id_, str(bank["version"]), bank)
        versions.append({"bank": bank, "ref": ref, "reviews": list(reviews), "change": change})
        return ref

    # ---- 수명 ----
    def start(self) -> "ReferenceServer":
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer._serve(self)

            do_POST = do_PUT = do_GET

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    # ---- 요청 처리 ----
    def _serve(self, h: BaseHTTPRequestHandler):
        n = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(n) if n else b""
        u = urlsplit(h.path)
        query = {k: v[-1] for k, v in parse_qs(u.query).items()}
        if self.delay:
            time.sleep(self.delay)
        auth = h.headers.get("Authorization", "")
        role = self.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        self.requests.append((h.command, h.path, role))
        try:
            if u.path in self.overrides:
                status, ctype, data, *extra = self.overrides[u.path]
            elif u.path in ("/healthz", "/readyz"):
                status, ctype, data, extra = 200, "application/json", b'{"ok":true}', []
            else:
                if role is None:
                    raise BoundaryError("PERMISSION_DENIED", "토큰이 없거나 틀리다")
                status, ctype, data, *extra = self._route(h.command, unquote(u.path), query, body, role)
        except BoundaryError as e:
            status, ctype, data, extra = STATUS[e.code], "application/json", json.dumps(e.to_record(V)).encode(), []
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        for k, v in (extra[0] if extra else {}).items():  # 선택: 네 번째 요소 = 추가 응답 헤더
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        try:
            h.wfile.write(data)
        except OSError:  # 클라이언트가 먼저 끊었다(타임아웃 시험)
            pass

    @staticmethod
    def _j(obj, status=200):
        return status, "application/json", json.dumps(obj, ensure_ascii=False).encode()

    @staticmethod
    def _page(items: list, q: dict):
        try:
            limit, start = int(q.get("limit", 100)), int(q.get("cursor") or 0)
            assert 1 <= limit <= 500 and start >= 0
        except (ValueError, AssertionError):
            raise BoundaryError("INVALID_RECORD", "cursor 또는 limit이 잘못됐다") from None
        end = start + limit
        return ReferenceServer._j({"items": items[start:end], "next_cursor": str(end) if end < len(items) else None})

    def _route(self, method, path, q, body, role):
        def need_write():
            if role == "reader":
                raise BoundaryError("PERMISSION_DENIED", "읽기 토큰으로는 쓸 수 없다")

        def nf(what):
            return BoundaryError("NOT_FOUND", f"{what}이 없다")

        def m(pattern):
            return re.fullmatch(pattern, path)

        J = self._j
        if method == "GET":
            # I1
            if path == "/v1/question-sets":
                return self._page(sorted(self.question_sets), q)
            if g := m(r"/v1/question-sets/([^/]+)/versions"):
                return self._page([v["ref"] for v in self.question_sets.get(g[1], [])], q)
            if g := m(r"/v1/question-sets/([^/]+)/latest"):
                vs = self.question_sets.get(g[1], [])
                if q.get("reviewed_only") == "true":
                    vs = [v for v in vs if v["reviews"] and v["reviews"][-1]["decision"] == "APPROVED"
                          and v["reviews"][-1]["clinical_basis_approved"]]
                if not vs:
                    raise nf("문항 세트")
                return J(vs[-1]["ref"])
            if g := m(r"/v1/question-sets/([^/]+)/versions/([^/]+)(|/reviews|/change)"):
                v = next((v for v in self.question_sets.get(g[1], []) if v["ref"]["version"] == g[2]), None)
                if v is None:
                    raise nf("문항 세트 버전")
                if g[3] == "/reviews":
                    return self._page(v["reviews"], q)
                return J({"item": v["change"]}) if g[3] == "/change" else J(v["bank"])
            # I3
            if path == "/v1/packages":
                return self._page(sorted({k[0] for k in self.packages}), q)
            if g := m(r"/v1/packages/([^/]+)/versions"):
                return self._page([k[1] for k in self.packages if k[0] == g[1]], q)
            if g := m(r"/v1/packages/([^/]+)/versions/([^/]+)(|/adapter)"):
                p = self.packages.get((g[1], g[2]))
                if p is None:
                    raise nf("패키지")
                return (200, "application/x-tar", make_tar(p["files"])) if g[3] else J(p["manifest"])
            if g := m(r"/v1/eval-reports/([^/]+)"):
                if g[1] not in self.eval_reports:
                    raise nf("평가 보고서")
                return 200, "application/octet-stream", self.eval_reports[g[1]]
            # I4
            if path == "/v1/active-pointer":
                return J({"item": self.pointer})
            if path == "/v1/releases":
                return self._page([self.releases[r]["manifest"] for r in reversed(self.release_order)], q)
            if g := m(r"/v1/releases/([^/]+)(|/bundle)"):
                r = self.releases.get(g[1])
                if r is None:
                    raise nf("릴리스")
                if not g[2]:
                    return J(r["manifest"])
                if not r["pass"]:
                    raise BoundaryError("FAILED_PRECONDITION", "PASS가 아닌 릴리스")
                files = {"manifest.json": json.dumps(r["manifest"]).encode(), "question_bank.json": json.dumps(r["bank"]).encode()}
                for role_, fs in r["adapters"].items():
                    files.update({f"adapters/{role_}/{p}": d for p, d in fs.items()})
                return 200, "application/x-tar", make_tar(files)
            if path == "/v1/decisions":
                keys = {"question_set_id": ("question_set", "id"), "question_set_version": ("question_set", "version"),
                        "lora_a_id": ("lora_a", "id"), "lora_a_version": ("lora_a", "version"),
                        "lora_b_id": ("lora_b", "id"), "lora_b_version": ("lora_b", "version")}
                out = [d for d in reversed(self.decisions)
                       if all(d["subject"][keys[k][0]][keys[k][1]] == v for k, v in q.items() if k in keys)
                       and ("submission_id" not in q or d.get("submission_id") == q["submission_id"])]
                return self._page(out, q)
            if g := m(r"/v1/decisions/([^/]+)"):
                d = next((d for d in self.decisions if d["decision_id"] == g[1]), None)
                if d is None:
                    raise nf("판정")
                return J(d)
            # I7
            if g := m(r"/v1/cases/([^/]+)"):
                cs = [c for c in self.cases if c["case_id"] == g[1] and ("version" not in q or str(c["version"]) == q["version"])]
                if not cs:
                    raise nf("케이스")
                return J(max(cs, key=lambda c: c["version"]))
            if path == "/v1/submissions":
                items = [_submission_view(x) for x in self.submissions.values()]
                if "status" in q:
                    items = [x for x in items if x["status"] == q["status"]]
                return self._page(items[::-1], q)
            if g := m(r"/v1/submissions/([^/]+)"):
                v = [x for x in self.submissions.values() if x["submission_id"] == g[1]]
                if not v:
                    raise nf("제출")
                return J(_submission_view(v[0]))
            if g := m(r"/v1/submissions/([^/]+)/review-report"):
                if not any(x["submission_id"] == g[1] for x in self.submissions.values()):
                    raise nf("제출")
                return J(dict(_example("ReviewReport"), submission_id=g[1]))
            if path == "/v1/cases":
                cs = self.cases
                for k in ("review_status", "failure_type", "source_release_id"):
                    if k in q:
                        cs = [c for c in cs if c.get(k) == q[k]]
                if "updated_since" in q:
                    cs = [c for c in cs if c["created_at"] >= q["updated_since"]]
                if q.get("latest_only", "true") == "true":
                    latest = {}
                    for c in cs:
                        if c["case_id"] not in latest or c["version"] > latest[c["case_id"]]["version"]:
                            latest[c["case_id"]] = c
                    cs = list(latest.values())
                return self._page(cs, q)
            if g := m(r"/v1/deidentified-traces/([^/]+)"):
                if g[1] not in self.deid_traces:
                    raise nf("비식별 Trace")
                return J(self.deid_traces[g[1]])
            raise nf("경로")
        # 쓰기
        need_write()
        jb = lambda: self._body(body)
        if path == "/v1/packages":
            return self._publish(body)
        if path == "/v1/submissions":
            b = jb()
            self._same_role(b, role)
            key = sha256_hash(b["subject"])
            if key not in self.submissions:
                self.submissions[key] = {"pipeline_schema_version": V, "submission_id": f"sub-{len(self.submissions) + 1}",
                                         "submitted_at": NOW, "submitted_by_role": role, "subject": b["subject"]}
            return J(self.submissions[key], 202)
        if path == "/v1/approvals":
            b = jb()
            if role != "release-approver":
                raise BoundaryError("PERMISSION_DENIED", "release-approver만 결정할 수 있다")
            if not any(x["submission_id"] == b.get("submission_id") for x in self.submissions.values()):
                raise nf("제출")
            if b.get("decision") == "REJECT" and not b.get("reason"):
                raise BoundaryError("INVALID_RECORD", "반려에는 사유가 필요하다")
            a = dict(_example("GateApproval"), submission_id=b["submission_id"], decision=b["decision"],
                     approver_id=b["approver_id"], approver_role=role)
            if b.get("reason"):
                a["reason"] = b["reason"]
            return J(a, 201)
        if path == "/v1/activate":
            b = jb()
            self._same_role(b, role)
            r = self.releases.get(b.get("release_id"))
            if r is None:
                raise nf("릴리스")
            if not r["pass"]:
                raise BoundaryError("FAILED_PRECONDITION", "PASS가 아닌 릴리스")
            if not self.pointer or self.pointer["release_id"] != b["release_id"]:
                self.pointer = {"pipeline_schema_version": V, "release_id": b["release_id"], "activated_at": NOW,
                                "pointer_version": (self.pointer or {"pointer_version": 0})["pointer_version"] + 1,
                                "cause": "ACTIVATE"}
            return J(self.pointer)
        if path == "/v1/rollback":
            b = jb()
            self._same_role(b, role)
            if b["request_id"] not in self.receipts:
                ok = (self.pointer and self.pointer["release_id"] == b["from_release_id"]
                      and self.releases.get(b["to_release_id"], {}).get("pass"))
                rec = {"pipeline_schema_version": V, "request_id": b["request_id"], "decided_at": NOW,
                       "status": "APPLIED" if ok else "REJECTED"}
                if ok:
                    self.pointer = {"pipeline_schema_version": V, "release_id": b["to_release_id"], "activated_at": NOW,
                                    "pointer_version": self.pointer["pointer_version"] + 1, "cause": "ROLLBACK"}
                    rec["active_pointer"] = self.pointer
                else:
                    rec["reason"] = "조건 불충족"
                self.receipts[b["request_id"]] = rec
            return J(self.receipts[b["request_id"]])
        if path == "/v1/traces":
            b = jb()
            key = (b["session_id"], b["turn_id"])
            status = "DUPLICATE" if key in self.traces else "ACCEPTED"
            self.traces.setdefault(key, b)
            return J({"pipeline_schema_version": V, "session_id": key[0], "turn_id": key[1], "status": status,
                      "received_at": NOW})
        raise nf("경로")

    @staticmethod
    def _body(body):
        try:
            b = json.loads(body)
            assert isinstance(b, dict)
            return b
        except (ValueError, AssertionError):
            raise BoundaryError("INVALID_RECORD", "본문이 JSON 객체가 아니다") from None

    @staticmethod
    def _same_role(b, role):
        if b.get("requested_by_role") != role:
            raise BoundaryError("PERMISSION_DENIED", "요청한 역할이 토큰의 역할과 다르다")

    def _publish(self, body):
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tf:
                files = {m.name: tf.extractfile(m).read() for m in tf if m.isreg()}
            manifest = json.loads(files["manifest.json"])
        except (tarfile.TarError, KeyError, ValueError):
            raise BoundaryError("INVALID_RECORD", "publish 본문이 잘못됐다") from None
        adapter = {n[len("adapter/"):]: d for n, d in files.items() if n.startswith("adapter/")}
        if adapter_digest(adapter) != manifest.get("adapter_hash"):
            raise BoundaryError("INTEGRITY_ERROR", "어댑터 해시가 맞지 않는다")
        key = (manifest["package_id"], manifest["version"])
        old = self.packages.get(key)
        if old:
            if old["manifest"]["content_hash"] != manifest["content_hash"]:
                raise BoundaryError("CONFLICT", "같은 ID·버전에 다른 내용")
            return self._j(old["manifest"])
        self.packages[key] = {"manifest": manifest, "files": adapter}
        self.eval_reports[manifest["eval_report_ref"]] = files.get("eval_report", b"")
        return self._j(manifest, 201)


class RuntimeReferenceServer(ReferenceServer):
    """HTTP.md I9(모델 추론 서버)를 따르는 참조 서버(설치된 계약 패키지 기준). I1~I7 서버와 경로(`/v1/releases/{id}`)가 겹치므로 따로 띄운다.

    실제 모델은 없다. `extract_fn(요청 dict) -> dict`와 `respond_fn(요청 dict) -> (문장, outcome)`을 바꿔 끼워 동작을 정한다.
    기본값은 예시의 고정 출력이다. `extract_fn`의 출력이 `ActionExtraction`을 어기면 서버는 그대로 내지 않고 503 OUTPUT_INVALID(D75)를 낸다. 요청 본문은 서버가 스키마로 검증하고(400 INVALID_RECORD), 적재되지 않은 릴리스는 404다.
    `available[릴리스 ID]`는 PUT 때의 결과다: "ok"(200 LOADED), "loading"(첫 PUT은 202 LOADING, 다음부터 LOADED),
    "not_pass"(412), 없으면 404. 토큰 역할 "reader"는 PUT(적재)을 할 수 없고 추론 POST는 할 수 있다.
    """

    def __init__(self, tokens: dict[str, str]):
        super().__init__(tokens)
        self.available: dict[str, str] = {}
        self.loaded: set[str] = set()
        self._loading: set[str] = set()
        self.extract_fn = lambda req: _example("ActionExtraction.next")
        self.respond_fn = lambda req: ("네, 알겠습니다. 다음 질문을 드리겠습니다.", "ok")
        self.attempts = 1
        self.protections = ""

    def _headers(self, outcome: str) -> dict:
        h = {"X-Openit-Outcome": outcome, "X-Openit-Attempts": str(self.attempts)}
        if self.protections:
            h["X-Openit-Protections"] = self.protections
        return h

    def _checked(self, body: bytes, schema: str) -> dict:
        from boundary_contracts import validator
        b = self._body(body)
        if list(validator(schema).iter_errors(b)):
            raise BoundaryError("INVALID_RECORD", f"본문이 {schema} 스키마를 위반한다")
        return b

    def _route(self, method, path, q, body, role):
        J = self._j
        if path == "/v1/info" and method == "GET":
            return J({"service": "model-runtime-reference", "runtime_version": "0.0.0", "supported_contract_versions": [1],
                      "backend": "fake", "loaded": sorted(self.loaded), "serving_config_hash": {}})
        g = re.fullmatch(r"/v1/releases/([^/]+)(|/extract|/respond)", path)
        if not g:
            raise BoundaryError("NOT_FOUND", "경로가 없다")
        rid, op = g[1], g[2]
        if not op:
            if method == "PUT":
                if role == "reader":
                    raise BoundaryError("PERMISSION_DENIED", "읽기 토큰으로는 적재할 수 없다")
                mode = self.available.get(rid)
                if mode is None:
                    raise BoundaryError("NOT_FOUND", "릴리스가 없다")
                if mode == "not_pass":
                    raise BoundaryError("FAILED_PRECONDITION", "승인되지 않은 릴리스")
                if mode == "loading" and rid not in self._loading and rid not in self.loaded:
                    self._loading.add(rid)
                    return J({"release_id": rid, "state": "LOADING", "error": None}, 202)
                self.loaded.add(rid)
                return J({"release_id": rid, "state": "LOADED", "error": None})
            if method == "GET":
                state = "LOADED" if rid in self.loaded else "LOADING" if rid in self._loading else "ABSENT"
                return J({"release_id": rid, "state": state, "error": None})
            raise BoundaryError("NOT_FOUND", "경로가 없다")
        if method != "POST":
            raise BoundaryError("NOT_FOUND", "경로가 없다")
        if rid not in self.loaded:
            raise BoundaryError("NOT_FOUND", "적재되지 않은 릴리스")
        if op == "/extract":
            req = self._checked(body, "ActionExtractionInput")
            out = self.extract_fn(req)
            from boundary_contracts import validator
            if list(validator("ActionExtraction").iter_errors(out)):
                # D75: 값을 지어내지 않는다. 모델 출력이 끝내 형식을 못 지키면 503 UNAVAILABLE + details.reason=OUTPUT_INVALID
                raise BoundaryError("UNAVAILABLE", "모델 출력이 ActionExtraction 형식을 지키지 못했다",
                                    {"reason": "OUTPUT_INVALID", "attempts": self.attempts})
            return 200, "application/json", json.dumps(out, ensure_ascii=False).encode(), self._headers("ok")
        req = self._checked(body, "ResponseRequest")
        text, outcome = self.respond_fn(req)
        return 200, "text/plain; charset=utf-8", text.encode("utf-8"), self._headers(outcome)
