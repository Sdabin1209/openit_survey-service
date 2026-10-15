"""추론 서버 클라이언트: 모델 런타임을 HTTP 경계 뒤에 둔다(`SURVEY_INFERENCE_URL`).

서비스는 모델을 적재하거나 호출하지 않고 이 HTTP 규약으로만 추론 서버를 부른다. 추론 서버(vLLM 컨테이너)는 model-runtime 영역이
구현한다. 규약은 `docs/inference-server-spec.md`가 정하고, 이 모듈은 그 클라이언트 쪽이다.

실패 처리(서버가 실제처럼 느리거나 죽거나 틀린 출력을 내는 경우)는 명세 5절 표를 따른다.
- 호출마다 타임아웃(`timeout`). 연결 실패·502/503/504는 `retries`번까지 짧게 다시 시도한다(추론은 부작용이 없다).
  읽기 타임아웃은 다시 시도하지 않는다(서버가 바쁘다는 뜻이라 지연만 두 배가 된다).
- 서버가 400을 주면(입력이 계약을 어김) 결정적이므로 다시 시도하지 않는다.
- 응답이 출력 계약(ParserOutput, GeneratorOutput)을 어기면 서버 결함이다. 서비스가 대체 출력을 만들지 않고 그 단계를 실패시킨다.
  출력 보호(D36)는 서버 쪽 model-runtime의 책임이다.
- 어느 경우든 예외는 Orchestrator가 그 단계(LoRA-A 또는 LoRA-B)의 실패로 처리한다.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Optional

import jsonschema

import survey_schema

from .errors import SurveyServiceError


class RemoteInferenceError(SurveyServiceError):
    """추론 서버 호출 실패. code는 BoundaryError 코드(UNAVAILABLE 등), timed_out은 읽기 타임아웃 여부."""
    def __init__(self, code: str, message: str, *, timed_out: bool = False, status: Optional[int] = None,
                 connect_failed: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.timed_out, self.status, self.connect_failed = code, timed_out, status, connect_failed


class _Http:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
            raise ValueError("base_url은 http:// 또는 https://로 시작해야 한다")
        self.base, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: Optional[Mapping] = None, timeout: Optional[float] = None):
        """(상태 코드, 본문 dict, 응답 헤더). 2xx가 아니거나 전송이 실패하면 RemoteInferenceError."""
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw), r.headers
                except ValueError:
                    raise RemoteInferenceError("INTEGRITY_ERROR", "추론 서버 응답이 JSON이 아니다", status=r.status) from None
        except urllib.error.HTTPError as e:
            try:
                rec = json.loads(e.read())
                code, msg = rec.get("code", "UNAVAILABLE"), str(rec.get("message", ""))
            except (ValueError, AttributeError):
                code, msg = ("UNAVAILABLE" if e.code >= 500 else "INVALID_RECORD"), ""
            if e.code in (502, 503, 504):
                code = "UNAVAILABLE"
            raise RemoteInferenceError(code, f"추론 서버가 {e.code}를 돌려줬다 {msg}"[:200], status=e.code) from None
        except (socket.timeout, TimeoutError):
            raise RemoteInferenceError("UNAVAILABLE", "추론 서버 응답이 시간 안에 오지 않았다", timed_out=True) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise RemoteInferenceError("UNAVAILABLE", "추론 서버 응답이 시간 안에 오지 않았다", timed_out=True) from None
            raise RemoteInferenceError("UNAVAILABLE", f"추론 서버에 연결할 수 없다 ({type(e.reason).__name__})", connect_failed=True) from None
        except (OSError, ValueError) as e:                      # 끊김, 깨진 응답
            raise RemoteInferenceError("UNAVAILABLE", f"추론 서버 호출이 끊겼다 ({type(e).__name__})") from None


class Stats:
    """호출 통계(원문 없음). /readyz에 낸다."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data = {"calls": 0, "failures": 0, "retries": 0, "timeouts": 0, "invalid_outputs": 0, "reloads": 0,
                     "outcome_repaired": 0, "outcome_fallback": 0}
        self.protections: dict[str, int] = {}      # 출력 보호 규칙 코드별 건수(P1, G5 ...). 원문 없음

    def add(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.data[key] += n

    def add_protections(self, header: str) -> None:
        """X-Openit-Protections: `P1:1,G5:1` 형식(규칙 코드:건수). 읽을 수 없는 조각은 건너뛴다."""
        with self._lock:
            for part in header.split(","):
                code, _, n = part.strip().partition(":")
                if code.isalnum() and n.isdigit():
                    self.protections[code] = self.protections.get(code, 0) + int(n)

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.data, "protections": dict(self.protections)}


class RemoteModelSession:
    """Orchestrator가 아는 parse/generate."""

    def __init__(self, http: _Http, release_id: str, *, retries: int = 1, backoff: float = 0.2, stats: Optional[Stats] = None,
                 version: int = 1) -> None:
        self._http, self.release_id, self.retries, self.backoff = http, release_id, retries, backoff
        self.stats = stats or Stats()
        self._v = {
            "parse": jsonschema.Draft202012Validator(survey_schema.load_schema("ParserOutput", version)),
            "generate": jsonschema.Draft202012Validator(survey_schema.load_schema("GeneratorOutput", version))}

    @property
    def _path(self) -> str:
        return f"/v1/releases/{urllib.parse.quote(self.release_id, safe='')}"

    def _call_once(self, op: str, body: Mapping):
        try:
            return self._http.call("POST", f"{self._path}/{op}", body)
        except RemoteInferenceError as e:
            if e.status != 404:
                raise
        # 추론 서버가 릴리스를 잊었다(재시작): 한 번 다시 적재를 요청한다. 오래 걸리면(202) 이 턴은 기다리지 않고 실패시킨다.
        self.stats.add("reloads")
        status, rec, _ = self._http.call("PUT", self._path)
        if status != 200 or rec.get("state") != "LOADED":
            raise RemoteInferenceError("UNAVAILABLE", "추론 서버가 릴리스를 다시 적재하는 중이다")
        return self._http.call("POST", f"{self._path}/{op}", body)

    def _post(self, op: str, body: Mapping) -> dict:
        self.stats.add("calls")
        attempt = 0
        while True:
            try:
                _, out, headers = self._call_once(op, body)
                break
            except RemoteInferenceError as e:
                if e.timed_out:
                    self.stats.add("timeouts")
                retriable = e.code == "UNAVAILABLE" and not e.timed_out
                if retriable and attempt < self.retries:
                    attempt += 1
                    self.stats.add("retries")
                    time.sleep(self.backoff * attempt)
                    continue
                self.stats.add("failures")
                raise
        if list(self._v[op].iter_errors(out)):
            self.stats.add("invalid_outputs")
            self.stats.add("failures")
            raise RemoteInferenceError("INTEGRITY_ERROR", "추론 서버 출력이 출력 계약을 어긴다")
        outcome = headers.get("X-Openit-Outcome") if headers is not None else None
        if outcome in ("repaired", "fallback"):
            self.stats.add("outcome_" + outcome)          # 출력 보호가 적용됐다(서버가 알린다). 건수만 센다
        prot = headers.get("X-Openit-Protections") if headers is not None else None
        if prot:
            self.stats.add_protections(prot)
        return out

    def parse(self, input: Mapping) -> dict:
        return self._post("parse", input)

    def generate(self, input: Mapping) -> dict:
        return self._post("generate", input)


class RemoteInference:
    """InferenceServer와 같은 load 인터페이스. 모델을 적재하는 대신 추론 서버에 릴리스 적재를 요청하고 끝나길 기다린다."""
    production_safe = True

    def __init__(self, base_url: str, token: str, timeout: float = 4.0, *, load_timeout: float = 600.0, retries: int = 1,
                 poll_interval: float = 0.5, backoff: float = 0.2) -> None:
        self.backend = "remote"
        self._http = _Http(base_url, token, timeout)
        self.load_timeout, self.retries, self.poll_interval, self.backoff = load_timeout, retries, poll_interval, backoff
        self.stats = Stats()

    def load(self, manifest: Mapping, adapters, *, contract_version=None) -> RemoteModelSession:
        from .inference import InferenceLoadError
        rid = manifest["release_id"]
        path = f"/v1/releases/{urllib.parse.quote(rid, safe='')}"
        deadline = time.monotonic() + self.load_timeout
        try:
            status, rec, _ = self._http.call("PUT", path, timeout=min(self.load_timeout, 60.0))
            while rec.get("state") != "LOADED":
                if rec.get("state") == "FAILED":
                    err = rec.get("error") or {}
                    raise InferenceLoadError("REMOTE_" + str(err.get("code", "FAILED_PRECONDITION")), "추론 서버가 릴리스 적재에 실패했다")
                if time.monotonic() > deadline:
                    raise InferenceLoadError("REMOTE_TIMEOUT", "추론 서버의 릴리스 적재가 시간 안에 끝나지 않았다")
                time.sleep(self.poll_interval)
                _, rec, _ = self._http.call("GET", path)
        except RemoteInferenceError as e:
            raise InferenceLoadError("REMOTE_" + e.code, "추론 서버가 릴리스 적재를 거부했거나 부를 수 없다") from e
        version = (contract_version or 1)
        return RemoteModelSession(self._http, rid, retries=self.retries, backoff=self.backoff, stats=self.stats, version=version)

    def ready(self) -> bool:
        try:
            req = urllib.request.Request(self._http.base + "/readyz")
            with self._http.opener.open(req, timeout=self._http.timeout) as r:
                return r.status == 200
        except Exception:
            return False
