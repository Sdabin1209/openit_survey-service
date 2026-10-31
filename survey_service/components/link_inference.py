"""추론 서버 클라이언트: 모델 런타임을 HTTP 경계 뒤에 둔다(`SURVEY_INFERENCE_URL`, HTTP.md I9 v2).

호출은 `boundary_contracts.clients.HttpModelRuntime`이 한다(D74): LoRA-A는 `extract`(JSON), LoRA-B는 `respond`(text/plain).
이 모듈은 그 위에서 호출 통계, 릴리스를 잊은 서버(404)의 다시 적재, 연결 실패의 한 번 재시도만 맡는다.
- 연결 실패·502/503/504류(UNAVAILABLE)는 한 번 짧게 다시 시도한다. 읽기 타임아웃은 다시 시도하지 않는다(서버가 바쁘다는 뜻이라 지연만 두 배가 된다).
- 503 OUTPUT_INVALID(D75: A 출력이 끝내 형식을 못 지킴)는 여기서 재시도하지 않는다. 호출자(graph)가 검증 위반과 같은 규칙으로 한 번 재시도한다.
- 서버가 400을 주면(입력이 계약을 어김) 결정적이므로 다시 시도하지 않는다.
- 어느 경우든 예외는 BoundaryError이고 graph가 그 단계(LoRA-A 또는 LoRA-B)의 실패로 처리한다.
"""
from __future__ import annotations


import collections
import logging
import threading
from typing import Callable, Mapping, Optional

from boundary_contracts import BoundaryError
from openit_link.clients import HttpModelRuntime, RuntimeResult, is_output_invalid

import json
import re
from pathlib import Path
from typing import Any, Callable

import model_runtime
import survey_schema


log = logging.getLogger("survey_service.remote")


class InferenceLoadError(BoundaryError):
    """모델을 적재할 수 없다. load_code는 거부 사유(상태·런북에 쓴다). load_retryable: 일시적이라 같은 릴리스를 나중에 다시 시도할 수 있는 실패(추론 서버가 아직 뜨는 중 등).
    BoundaryError의 code·retryable과 이름이 겹치지 않게 load_ 접두를 쓴다."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__("UNAVAILABLE" if retryable else "FAILED_PRECONDITION", f"{code}: {message}",
                         {"reason": "INFERENCE_LOAD", "load_code": code, "load_retryable": retryable})
        self.load_code, self.load_retryable = code, retryable


def _timed_out(e: BoundaryError, elapsed: float, timeout: float) -> bool:
    """타임아웃인가. boundary_contracts 클라이언트는 응답을 읽다 시간이 넘으면 reason=TimeoutError, 연결 단계에서 넘으면 urllib이
    URLError로 감싸 reason=URLError로 낸다. 후자는 연결 실패(거부, 이름 풀이 실패)와 구분되지 않으므로 걸린 시간으로 가른다.
    서버가 응답을 줬거나(details.status가 있다) 이 서비스가 직접 낸 UNAVAILABLE(reason=RELOADING 등)은 느렸어도 타임아웃이 아니다:
    예전에는 걸린 시간만 봐서 느린 502·503 응답이 타임아웃으로 세어지고 재시도도 건너뛰었다."""
    d = e.details or {}
    reason = str(d.get("reason", ""))
    if "imeout" in reason:
        return True
    if d.get("status") is not None or (reason and reason != "URLError"):
        return False
    return reason == "URLError" and elapsed >= timeout * 0.9


def _failure_info(e: BoundaryError) -> str:
    d = e.details or {}
    return f"code={e.code} reason={d.get('reason', '-')} status={d.get('status', '-')}"


class Stats:
    """호출 통계(원문 없음). /readyz에 낸다."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data = {"calls": 0, "failures": 0, "retries": 0, "retry_skipped_budget": 0, "timeouts": 0, "output_invalid": 0, "reloads": 0, "outcome_fallback": 0}
        self.protections: dict[str, int] = {}      # 출력 보호 규칙 코드별 건수(G0, G5 ...). 원문 없음
        self._calls: "collections.deque[dict]" = collections.deque(maxlen=64)      # 최근 호출 기록(호출 하나의 경과, 원문 없음): 시험이 '끊긴 시점'을 턴 전체 벽시계가 아니라 호출 경과로 잰다

    def record_call(self, op: str, attempt: int, elapsed: float, ok: bool, timed_out: bool) -> None:
        with self._lock:
            self._calls.append({"op": op, "attempt": attempt, "elapsed": elapsed, "ok": ok, "timed_out": timed_out})

    def calls(self) -> list:
        with self._lock:
            return list(self._calls)

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

    accepts_deadline = True      # graph가 턴 마감(단조 시계 기준)을 넘겨 준다: 재시도 판단에 쓴다

    def __init__(self, runtime: HttpModelRuntime, release_id: str, *, retries: int = 1, backoff: float = 0.2, stats: Optional[Stats] = None,
                 op_runtimes: Optional[Mapping[str, HttpModelRuntime]] = None,
                 monotonic: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._now, self._sleep = monotonic, sleep      # 시계 주입(ADR-0050): 재시도 예산과 백오프 대기를 시험이 가짜 시계로 흘린다. 기본은 실제 시계
        self._rt, self.release_id, self.retries, self.backoff = runtime, release_id, retries, backoff
        self._ops = dict(op_runtimes or {})      # 호출 종류별 클라이언트(호출별 상한이 다르다). 없으면 runtime 하나를 쓴다
        self.stats = stats or Stats()

    def _op_rt(self, op: str) -> HttpModelRuntime:
        return self._ops.get(op, self._rt)

    def _call_once(self, op: str, request: Mapping, flow: Optional[Mapping] = None, fields: Optional[Mapping] = None) -> RuntimeResult:
        fn = getattr(self._op_rt(op), op)
        kw = {"flow": dict(flow), "fields": dict(fields)} if op == "extract" else {}      # extract는 항상 flow·fields와 부른다(계약 3.0.0: ExtractRequest{input, flow, fields}만)
        try:
            return fn(self.release_id, dict(request), **kw)
        except BoundaryError as e:
            if e.code != "NOT_FOUND":
                raise
        # 추론 서버가 릴리스를 잊었다(재시작): 한 번 다시 적재를 요청한다. 오래 걸리면(LOADING) 이 턴은 기다리지 않고 실패시킨다.
        self.stats.add("reloads")
        st = self._rt.load_release(self.release_id)
        if st["state"] != "LOADED":
            raise BoundaryError("UNAVAILABLE", "추론 서버가 릴리스를 다시 적재하는 중이다", {"reason": "RELOADING"})
        return fn(self.release_id, dict(request), **kw)

    def _call(self, op: str, request: Mapping, deadline: Optional[float] = None, flow: Optional[Mapping] = None,
              fields: Optional[Mapping] = None) -> RuntimeResult:
        self.stats.add("calls")
        limit = self._op_rt(op)._timeout
        attempt = 0
        while True:
            t0 = self._now()
            try:
                res = self._call_once(op, request, flow, fields)
                self.stats.record_call(op, attempt + 1, self._now() - t0, True, False)
                break
            except BoundaryError as e:
                if is_output_invalid(e):
                    self.stats.record_call(op, attempt + 1, self._now() - t0, False, False)
                    self.stats.add("output_invalid")
                    self.stats.add("failures")
                    raise
                elapsed = self._now() - t0
                timed_out = e.code == "UNAVAILABLE" and _timed_out(e, elapsed, limit)
                self.stats.record_call(op, attempt + 1, elapsed, False, timed_out)
                log.warning("추론 호출 실패 op=%s %s elapsed=%.2fs read_timeout=%.1fs classified=%s attempt=%d", op, _failure_info(e),
                            elapsed, limit, "timeout" if timed_out else "not_timeout", attempt + 1)
                if timed_out:
                    self.stats.add("timeouts")
                # 왜: 재시도는 남은 턴 예산이 이 호출의 상한 이상일 때만 한다(계약 CONVENTIONS 2.2.0). 모자라면 재시도하지 않고 실패로 둔다.
                #     백오프 대기도 예산에서 쓴다. deadline이 없으면(턴 마감을 끈 구성) 횟수 제한만 본다.
                if deadline is not None and e.code == "UNAVAILABLE" and not timed_out and attempt < self.retries and \
                        deadline - self._now() - self.backoff * (attempt + 1) < limit:
                    self.stats.add("retry_skipped_budget")
                    self.stats.add("failures")
                    raise
                if e.code == "UNAVAILABLE" and not timed_out and attempt < self.retries:
                    attempt += 1
                    self.stats.add("retries")
                    self._sleep(self.backoff * attempt)
                    continue
                self.stats.add("failures")
                raise
        if res.is_fallback:
            self.stats.add("outcome_fallback")
        if res.protections:
            self.stats.add_protections(res.protections)
        return res

    def extract(self, request: Mapping, deadline: Optional[float] = None, *, flow: Mapping, fields: Mapping) -> RuntimeResult:
        return self._call("extract", request, deadline, flow, fields)

    def respond(self, request: Mapping, deadline: Optional[float] = None) -> RuntimeResult:
        return self._call("respond", request, deadline)


class RemoteInference:
    """InferenceServer와 같은 load 인터페이스. 모델을 적재하는 대신 추론 서버에 릴리스 적재를 요청하고 끝나길 기다린다."""
    production_safe = True
    needs_adapters = False     # 어댑터는 추론 서버(model-runtime)가 직접 받는다. 서비스는 문항 세트만 받는다 (ADR-0040)

    def __init__(self, base_url: str, token: str, timeout: float = 4.0, *, load_timeout: float = 600.0, retries: int = 1,
                 poll_interval: float = 0.5, backoff: float = 0.2, extract_timeout: float = 3.0, respond_timeout: float = 5.0,
                 monotonic: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._now, self._sleep = monotonic, sleep      # 시계 주입(ADR-0050)
        self.backend = "remote"
        self._rt = HttpModelRuntime(base_url, token, timeout)          # 제어 호출(적재, 상태, readyz)
        # 환자 턴의 호출은 종류마다 상한이 다르다(extract 3.0초, respond 2.0초: 계약 CONVENTIONS 2.2.0 배분, 측정 안 함, ADR-0026)
        self._op_rts = {"extract": HttpModelRuntime(base_url, token, extract_timeout), "respond": HttpModelRuntime(base_url, token, respond_timeout)}
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self.load_timeout, self.retries, self.poll_interval, self.backoff = load_timeout, retries, poll_interval, backoff
        self.stats = Stats()

    def load(self, manifest: Mapping, adapters, *, contract_version=None) -> RemoteModelSession:
        rid = manifest["release_id"]
        deadline = self._now() + self.load_timeout
        try:
            rec = self._rt.load_release(rid)
            while rec["state"] != "LOADED":
                if rec["state"] == "FAILED":
                    code = str((rec.get("error") or {}).get("code", "FAILED_PRECONDITION"))
                    raise InferenceLoadError("REMOTE_" + code, "추론 서버가 릴리스 적재에 실패했다", retryable=code == "UNAVAILABLE")
                if self._now() > deadline:   # 서버가 계속 적재 중이다: 일시적이다(같은 PUT은 멱등이라 나중에 다시 하면 된다)
                    raise InferenceLoadError("REMOTE_TIMEOUT", "추론 서버의 릴리스 적재가 시간 안에 끝나지 않았다", retryable=True)
                self._sleep(self.poll_interval)
                rec = self._rt.get_release_state(rid)
        except InferenceLoadError:
            raise
        except BoundaryError as e:
            # 연결 실패, 타임아웃, 502·503·504는 UNAVAILABLE이다: 추론 서버가 아직 뜨는 중일 수 있어 다시 시도할 수 있다.
            # 412·422·403·404 등 결정적 거부는 다시 시도해도 같다.
            raise InferenceLoadError("REMOTE_" + e.code, "추론 서버가 릴리스 적재를 거부했거나 부를 수 없다",
                                     retryable=e.code == "UNAVAILABLE") from e
        return RemoteModelSession(self._rt, rid, retries=self.retries, backoff=self.backoff, stats=self.stats, op_runtimes=self._op_rts, monotonic=self._now, sleep=self._sleep)

    def ready(self) -> bool:
        import urllib.request
        try:
            req = urllib.request.Request(self._base + "/readyz")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self._timeout) as r:
                return r.status == 200
        except Exception:
            return False


# ---------------------------------------------------------------- 서비스 안 적재(개발·스모크 전용)

KNOWN_BACKENDS = ("fake",)


def role_configs(manifest: Mapping) -> tuple[dict, dict, dict]:
    """역할별 serving_config, 해시, 시스템 프롬프트 참조. 매니페스트가 역할마다 가진 그대로 I9 load에 넘긴다(D45, D75)."""
    ad = manifest["adapters"]
    roles = ("LORA_A", "LORA_B")
    return ({r: ad[r]["serving_config"] for r in roles}, {r: ad[r]["serving_config_hash"] for r in roles},
            {r: ad[r]["system_prompt"] for r in roles})


def dev_responder(req) -> str:
    """개발·스모크용 가짜 백엔드의 출력. LoRA-A는 항상 "관계없는 말"(REDIRECT, OFF_TOPIC)로 같은 문항을 유지한다: 어떤 입력에도
    validate_extraction을 통과하고 세션이 끝나지 않는다(model_runtime의 기본 가짜 출력은 NEXT -> END라 검증을 통과하지 못한다)."""
    if req.role == "LORA_A":
        m = re.search(r"<\|im_start\|>user\n(.*)<\|im_end\|>", req.prompt, re.S)
        qid = json.loads(m.group(1))["question_id"]
        return json.dumps({"action": "REDIRECT", "next_item": qid, "reason": "OFF_TOPIC", "values": []}, ensure_ascii=False)
    return "(가짜 응답)"


class LocalModelSession:
    """model-runtime ModelSession을 graph가 아는 모양(RuntimeResult, BoundaryError)으로 맞춘다."""

    def __init__(self, session) -> None:
        self._s = session

    @property
    def serving_config_hashes(self):
        return self._s.serving_config_hashes

    def extract(self, request: Mapping, *, flow: Mapping, fields: Mapping) -> RuntimeResult:
        # model-runtime 0.11.0부터 세션의 extract도 흐름 규칙과 값 규칙을 보증한다(서버와 같은 코드): flow·fields를 그대로 넘긴다.
        try:
            out = self._s.extract(request, flow, fields)
        except model_runtime.ModelOutputInvalid as e:
            raise BoundaryError("UNAVAILABLE", "모델 출력이 ActionExtraction 형식을 지키지 못했다", {"reason": "OUTPUT_INVALID"}) from e
        info = self._s.last_call_info
        return RuntimeResult(out, "ok", info.attempts if info else None, dict(info.protections) if info else {})

    def respond(self, request: Mapping) -> RuntimeResult:
        r = self._s.respond(request)
        info = self._s.last_call_info
        return RuntimeResult(r.text, "fallback" if r.is_fallback else "ok", info.attempts if info else None,
                             dict(info.protections) if info else {})


class InferenceServer:
    needs_adapters = True      # 서비스 안에서 모델(가짜 백엔드)을 올리므로 어댑터 파일이 필요하다: 릴리스 번들을 받는다 (ADR-0040)
    def __init__(self, backend: str, *, contract_version: int = 1, backend_factory: Optional[Callable[[], Any]] = None) -> None:
        if backend not in KNOWN_BACKENDS and backend_factory is None:
            raise InferenceLoadError("UNKNOWN_BACKEND", f"구현된 백엔드가 아니다: {backend!r} (있는 것: {KNOWN_BACKENDS})")
        self.backend = backend
        self.contract_version = contract_version
        self._backend_factory = backend_factory
        self.production_safe = False if backend == "fake" else True

    def _new_backend(self):
        if self._backend_factory is not None:
            return self._backend_factory()
        if self.backend == "fake":
            return model_runtime.FakeBackend(dev_responder)
        return None

    def load(self, manifest: Mapping, adapters: Mapping[str, Path], *, contract_version: Optional[int] = None) -> LocalModelSession:
        """ReleaseManifest와 풀어 둔 어댑터로 ModelSession을 만든다. model-runtime의 거부는 InferenceLoadError로 감싼다."""
        configs, hashes, prompts = role_configs(manifest)
        ver = contract_version or self.contract_version
        try:
            return LocalModelSession(model_runtime.load(dict(adapters), configs, schema_dir=survey_schema.schema_dir(ver),
                                                        expected_serving_config_hashes=hashes, system_prompts=prompts,
                                                        backend=self._new_backend()))
        except model_runtime.ModelRuntimeError as e:
            raise InferenceLoadError(type(e).__name__, "model-runtime이 적재를 거부했다") from e
        except Exception as e:  # ContractViolation 등. 값은 담지 않는다.
            raise InferenceLoadError(type(e).__name__, "모델 적재 실패") from e
