"""추론 서버: 릴리스마다 ModelSession을 만들어 들고 있다.

지금은 서비스 프로세스 안의 구성이다(설계 7절). Orchestrator는 parse/generate를 가진 ModelSession만 안다. 실제 백엔드(GPU)는
model-runtime에 아직 없어서 `fake`(개발·스모크 전용)만 고를 수 있다. 값이 없거나 모르는 백엔드는 시작을 거부한다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import model_runtime
import survey_schema

from .errors import SurveyServiceError


class InferenceLoadError(SurveyServiceError):
    """모델을 적재할 수 없다. code는 거부 사유(상태·런북에 쓴다)."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


KNOWN_BACKENDS = ("fake",)


def role_configs(manifest: Mapping) -> tuple[dict, dict]:
    """역할별 serving_config와 해시. 매니페스트가 역할마다 가진 그대로 I9 load(0.3.0)에 넘긴다(D45)."""
    ad = manifest["adapters"]
    return ({r: ad[r]["serving_config"] for r in ("LORA_A", "LORA_B")},
            {r: ad[r]["serving_config_hash"] for r in ("LORA_A", "LORA_B")})


class InferenceServer:
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
            return model_runtime.FakeBackend()
        return None

    def load(self, manifest: Mapping, adapters: Mapping[str, Path], *, contract_version: Optional[int] = None):
        """ReleaseManifest와 풀어 둔 어댑터로 ModelSession을 만든다.

        I9 load(model-runtime 0.3.0)는 역할별 serving_configs와 기대 해시를 받는다. 한 역할만 어긋나도 model-runtime이 거부한다.
        model-runtime의 거부(ServingConfigMismatch 등)는 InferenceLoadError로 감싸 이전 릴리스를 유지하게 한다.
        """
        configs, hashes = role_configs(manifest)
        ver = contract_version or self.contract_version
        try:
            return model_runtime.load(dict(adapters), configs, schema_dir=survey_schema.schema_dir(ver),
                                      expected_serving_config_hashes=hashes, backend=self._new_backend())
        except model_runtime.ModelRuntimeError as e:
            raise InferenceLoadError(type(e).__name__, "model-runtime이 적재를 거부했다") from e
        except Exception as e:  # ContractViolation 등. 값은 담지 않는다.
            raise InferenceLoadError(type(e).__name__, "모델 적재 실패") from e
