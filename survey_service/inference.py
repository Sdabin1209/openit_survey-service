"""프로세스 안 추론(개발·스모크 전용): 릴리스마다 model-runtime의 ModelSession을 만들어 들고 있다.

실제 운영은 `remote.py`(HTTP 추론 서버, model-runtime 서버)다. 여기서는 model-runtime의 FakeBackend만 쓸 수 있다.
값이 없거나 모르는 백엔드는 시작을 거부한다. graph는 extract/respond가 `RuntimeResult`를 돌려주는 세션만 안다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import json
import re

import model_runtime
import survey_schema
from boundary_contracts import BoundaryError
from boundary_contracts.clients import RuntimeResult

from .errors import SurveyServiceError


class InferenceLoadError(SurveyServiceError):
    """모델을 적재할 수 없다. code는 거부 사유(상태·런북에 쓴다)."""
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable      # 일시적이라 같은 릴리스를 나중에 다시 시도할 수 있는 실패(추론 서버가 아직 뜨는 중 등)


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

    def extract(self, request: Mapping) -> RuntimeResult:
        try:
            out = self._s.extract(request)
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
