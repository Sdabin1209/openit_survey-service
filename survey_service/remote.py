"""추론 서버 클라이언트: 모델 런타임을 HTTP 경계 뒤에 둔다(`SURVEY_INFERENCE_URL`, HTTP.md I9 v2).

호출은 `boundary_contracts.clients.HttpModelRuntime`이 한다(D74): LoRA-A는 `extract`(JSON), LoRA-B는 `respond`(text/plain).
이 모듈은 그 위에서 호출 통계, 릴리스를 잊은 서버(404)의 다시 적재, 연결 실패의 한 번 재시도만 맡는다.
- 연결 실패·502/503/504류(UNAVAILABLE)는 한 번 짧게 다시 시도한다. 읽기 타임아웃은 다시 시도하지 않는다(서버가 바쁘다는 뜻이라 지연만 두 배가 된다).
- 503 OUTPUT_INVALID(D75: A 출력이 끝내 형식을 못 지킴)는 여기서 재시도하지 않는다. 호출자(graph)가 검증 위반과 같은 규칙으로 한 번 재시도한다.
- 서버가 400을 주면(입력이 계약을 어김) 결정적이므로 다시 시도하지 않는다.
- 어느 경우든 예외는 BoundaryError이고 graph가 그 단계(LoRA-A 또는 LoRA-B)의 실패로 처리한다.
"""
from __future__ import annotations

import threading
import time
from typing import Mapping, Optional

from boundary_contracts import BoundaryError
from boundary_contracts.clients import HttpModelRuntime, RuntimeResult, is_output_invalid

from .inference import InferenceLoadError


def _timed_out(e: BoundaryError) -> bool:
    return "imeout" in str(e.details.get("reason", "")) or "imed out" in e.message


class Stats:
    """호출 통계(원문 없음). /readyz에 낸다."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data = {"calls": 0, "failures": 0, "retries": 0, "timeouts": 0, "output_invalid": 0, "reloads": 0, "outcome_fallback": 0}
        self.protections: dict[str, int] = {}      # 출력 보호 규칙 코드별 건수(G0, G5 ...). 원문 없음

    def add(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.data[key] += n

    def add_protections(self, prot: Mapping[str, int]) -> None:
        with self._lock:
            for code, n in prot.items():
                self.protections[code] = self.protections.get(code, 0) + n

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.data, "protections": dict(self.protections)}


class RemoteModelSession:
    """graph가 아는 extract/respond. 둘 다 RuntimeResult(output, outcome, attempts, protections)를 돌려준다."""

    def __init__(self, runtime: HttpModelRuntime, release_id: str, *, retries: int = 1, backoff: float = 0.2, stats: Optional[Stats] = None) -> None:
        self._rt, self.release_id, self.retries, self.backoff = runtime, release_id, retries, backoff
        self.stats = stats or Stats()

    def _call_once(self, op: str, request: Mapping) -> RuntimeResult:
        fn = getattr(self._rt, op)
        try:
            return fn(self.release_id, dict(request))
        except BoundaryError as e:
            if e.code != "NOT_FOUND":
                raise
        # 추론 서버가 릴리스를 잊었다(재시작): 한 번 다시 적재를 요청한다. 오래 걸리면(LOADING) 이 턴은 기다리지 않고 실패시킨다.
        self.stats.add("reloads")
        st = self._rt.load_release(self.release_id)
        if st["state"] != "LOADED":
            raise BoundaryError("UNAVAILABLE", "추론 서버가 릴리스를 다시 적재하는 중이다")
        return fn(self.release_id, dict(request))

    def _call(self, op: str, request: Mapping) -> RuntimeResult:
        self.stats.add("calls")
        attempt = 0
        while True:
            try:
                res = self._call_once(op, request)
                break
            except BoundaryError as e:
                if is_output_invalid(e):
                    self.stats.add("output_invalid")
                    self.stats.add("failures")
                    raise
                if _timed_out(e):
                    self.stats.add("timeouts")
                if e.code == "UNAVAILABLE" and not _timed_out(e) and attempt < self.retries:
                    attempt += 1
                    self.stats.add("retries")
                    time.sleep(self.backoff * attempt)
                    continue
                self.stats.add("failures")
                raise
        if res.is_fallback:
            self.stats.add("outcome_fallback")
        if res.protections:
            self.stats.add_protections(res.protections)
        return res

    def extract(self, request: Mapping) -> RuntimeResult:
        return self._call("extract", request)

    def respond(self, request: Mapping) -> RuntimeResult:
        return self._call("respond", request)


class RemoteInference:
    """InferenceServer와 같은 load 인터페이스. 모델을 적재하는 대신 추론 서버에 릴리스 적재를 요청하고 끝나길 기다린다."""
    production_safe = True

    def __init__(self, base_url: str, token: str, timeout: float = 4.0, *, load_timeout: float = 600.0, retries: int = 1,
                 poll_interval: float = 0.5, backoff: float = 0.2) -> None:
        self.backend = "remote"
        self._rt = HttpModelRuntime(base_url, token, timeout)
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self.load_timeout, self.retries, self.poll_interval, self.backoff = load_timeout, retries, poll_interval, backoff
        self.stats = Stats()

    def load(self, manifest: Mapping, adapters, *, contract_version=None) -> RemoteModelSession:
        rid = manifest["release_id"]
        deadline = time.monotonic() + self.load_timeout
        try:
            rec = self._rt.load_release(rid)
            while rec["state"] != "LOADED":
                if rec["state"] == "FAILED":
                    code = str((rec.get("error") or {}).get("code", "FAILED_PRECONDITION"))
                    raise InferenceLoadError("REMOTE_" + code, "추론 서버가 릴리스 적재에 실패했다", retryable=code == "UNAVAILABLE")
                if time.monotonic() > deadline:   # 서버가 계속 적재 중이다: 일시적이다(같은 PUT은 멱등이라 나중에 다시 하면 된다)
                    raise InferenceLoadError("REMOTE_TIMEOUT", "추론 서버의 릴리스 적재가 시간 안에 끝나지 않았다", retryable=True)
                time.sleep(self.poll_interval)
                rec = self._rt.get_release_state(rid)
        except BoundaryError as e:
            # 연결 실패, 타임아웃, 502·503·504는 UNAVAILABLE이다: 추론 서버가 아직 뜨는 중일 수 있어 다시 시도할 수 있다.
            # 412·422·403·404 등 결정적 거부는 다시 시도해도 같다.
            raise InferenceLoadError("REMOTE_" + e.code, "추론 서버가 릴리스 적재를 거부했거나 부를 수 없다",
                                     retryable=e.code == "UNAVAILABLE") from e
        return RemoteModelSession(self._rt, rid, retries=self.retries, backoff=self.backoff, stats=self.stats)

    def ready(self) -> bool:
        import urllib.request
        try:
            req = urllib.request.Request(self._base + "/readyz")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self._timeout) as r:
                return r.status == 200
        except Exception:
            return False
