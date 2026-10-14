"""시험용 대역. 이웃 영역(release-gate, ops-feedback)의 실제 구현이 없어 boundary_contracts.sdk의 Protocol을 따라 직접 만든다."""
from __future__ import annotations

import copy
import shutil
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from boundary_contracts import BoundaryError
from boundary_contracts.sdk import ReleaseBundle


class RecordingConsole:
    def __init__(self, fail: bool = False) -> None:
        self.inputs: list = []
        self.fail = fail

    def deliver(self, console_input: Mapping) -> None:
        self.inputs.append(copy.deepcopy(console_input))
        if self.fail:
            raise RuntimeError("직원 콘솔 전달 실패(시험)")


class RecordingDispatcher:
    def __init__(self, output: Optional[Mapping] = None, fail: bool = False) -> None:
        self.inputs: list = []
        self.output = output or {"dispatch_record": {"succeeded": True}}
        self.fail = fail

    def dispatch(self, dispatch_input: Mapping) -> Mapping:
        self.inputs.append(copy.deepcopy(dispatch_input))
        if self.fail:
            raise RuntimeError("결과 전달 실패(시험)")
        return copy.deepcopy(self.output)


class RecordingSink:
    def __init__(self, fail: bool = False) -> None:
        self.inputs: list = []
        self.fail = fail

    def deliver(self, frontend_input: Mapping) -> None:
        self.inputs.append(copy.deepcopy(frontend_input))
        if self.fail:
            raise RuntimeError("프론트 전달 실패(시험)")


class FaultyStore:
    """진짜 저장소를 감싸 지정한 동작을 실패시킨다."""
    def __init__(self, inner, *, fail_create: bool = False, fail_commit: Optional[str] = None, fail_load: bool = False,
                 fail_append_log: bool = False) -> None:
        self._inner = inner
        self.fail_create, self.fail_commit, self.fail_load, self.fail_append_log = fail_create, fail_commit, fail_load, fail_append_log

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create(self, *a, **k):
        if self.fail_create:
            from survey_service.errors import StoreError
            raise StoreError("만들 수 없다(시험)")
        return self._inner.create(*a, **k)

    def load(self, session_id):
        if self.fail_load:
            return {"session_id": session_id, "status": "DB_ERROR", "revision": None, "state": None}
        return self._inner.load(session_id)

    def append_log(self, *a, **k):
        if self.fail_append_log:
            from survey_service.errors import StoreError
            raise StoreError("쓸 수 없다(시험)")
        return self._inner.append_log(*a, **k)

    def commit(self, request, **k):
        if self.fail_commit:
            outcome = "UNKNOWN" if self.fail_commit == "UNKNOWN" else "NOT_APPLIED"
            return {"session_id": request["session_id"], "status": "DB_ERROR", "write_outcome": outcome, "revision": None,
                    "detail": "시험용 실패"}
        return self._inner.commit(request, **k)


# ---------------------------------------------------------------- I4 ReleaseReader (release-gate의 메모리 구현 대역)
class FakeReleaseReader:
    """boundary_contracts.sdk.ReleaseReader Protocol을 따르는 시험용 대역. 실제 구현은 release-gate가 배포한다."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifests: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}
        self.banks: dict[str, dict] = {}
        self.pointer: Optional[dict] = None
        self.errors: dict[str, list] = {}          # 메서드 이름 -> 다음 호출에서 던질 BoundaryError 목록
        self.calls: dict[str, int] = {}

    def _maybe_fail(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        q = self.errors.get(name)
        if q:
            e = q.pop(0)
            if e is not None:
                raise e

    def add_release(self, release_id: str, bank: dict, *, runtime_contract_version: int = 1, decision_result: str = "PASS",
                    serving_config_b: Optional[dict] = None, turn_engine_version: str = "0.2.0", model_runtime_version: str = "0.2.0",
                    decision_subject_swap: bool = False, tamper_bank: bool = False) -> dict:
        from boundary_contracts import canonical
        import model_runtime
        cfg = model_runtime.fake_serving_config()
        dirs = {}
        for role in ("LORA_A", "LORA_B"):
            d = self.root / "adapters" / release_id / role
            d.mkdir(parents=True, exist_ok=True)
            (d / "adapter.bin").write_bytes(f"{release_id}-{role}".encode())
            dirs[role] = d
        cfg_b = serving_config_b or cfg
        ref = lambda i, v, h: {"id": i, "version": v, "content_hash": h}
        zero = "sha256:" + "0" * 64
        manifest = {
            "pipeline_schema_version": "1.1", "release_id": release_id, "created_at": "2026-10-15T00:00:00Z",
            "gate_decision_id": f"gd-{release_id}",
            "question_set": ref("qs-main", str(bank["version"]), canonical.sha256_hash(bank)),
            "lora_a": ref("lora-a", "1.0.0", zero), "lora_b": ref("lora-b", "1.0.0", zero),
            "runtime_contract_version": runtime_contract_version, "turn_engine_version": turn_engine_version,
            "model_runtime_version": model_runtime_version,
            "adapters": {
                "LORA_A": {"adapter_hash": canonical.directory_hash(dirs["LORA_A"]), "serving_config": cfg,
                           "serving_config_hash": model_runtime.serving_config_hash(cfg)},
                "LORA_B": {"adapter_hash": canonical.directory_hash(dirs["LORA_B"]), "serving_config": cfg_b,
                           "serving_config_hash": model_runtime.serving_config_hash(cfg_b)}}}
        manifest["content_hash"] = canonical.content_hash(manifest)
        subject = {k: manifest[k] for k in ("question_set", "lora_a", "lora_b")}
        if decision_subject_swap:
            subject = dict(subject, lora_a=ref("other", "9", zero))
        self.decisions[manifest["gate_decision_id"]] = {
            "pipeline_schema_version": "1.1", "decision_id": manifest["gate_decision_id"], "decided_at": "2026-10-15T00:00:00Z",
            "gate_policy_version": "1", "subject": subject, "runtime_contract_version": runtime_contract_version,
            "result": decision_result, "checks": [{"kind": "CONTRACT_COMPAT", "result": "PASS"}],
            "approver": {"role": "gate-service"}, "release_id": release_id}
        if decision_result == "REJECT":
            self.decisions[manifest["gate_decision_id"]]["reject_reasons"] = ["시험"]
        self.manifests[release_id] = manifest
        served = copy.deepcopy(bank)
        if tamper_bank:
            served["session_settings"]["confirm_limit"] += 1
        self.banks[release_id] = served
        return manifest

    def set_pointer(self, release_id: str, cause: str = "ACTIVATE") -> dict:
        v = (self.pointer["pointer_version"] + 1) if self.pointer else 1
        self.pointer = {"pipeline_schema_version": "1.1", "release_id": release_id, "pointer_version": v,
                        "activated_at": "2026-10-15T00:00:00Z", "cause": cause}
        return self.pointer

    # --- ReleaseReader Protocol
    def get_active_pointer(self):
        self._maybe_fail("get_active_pointer")
        return copy.deepcopy(self.pointer)

    def get_release(self, release_id: str):
        self._maybe_fail("get_release")
        if release_id not in self.manifests:
            raise BoundaryError("NOT_FOUND", "릴리스가 없다")
        return copy.deepcopy(self.manifests[release_id])

    def load_bundle(self, release_id: str, *, dest: Path):
        self._maybe_fail("load_bundle")
        m = self.manifests.get(release_id)
        if m is None:
            raise BoundaryError("NOT_FOUND", "릴리스가 없다")
        if self.decisions[m["gate_decision_id"]]["result"] != "PASS":
            raise BoundaryError("FAILED_PRECONDITION", "통과하지 않은 릴리스")
        adapters = {}
        for role in ("LORA_A", "LORA_B"):
            out = Path(dest) / role
            shutil.copytree(self.root / "adapters" / release_id / role, out, dirs_exist_ok=True)
            adapters[role] = out
        return ReleaseBundle(copy.deepcopy(m), copy.deepcopy(self.banks[release_id]), adapters)

    def get_decision(self, decision_id: str):
        self._maybe_fail("get_decision")
        if decision_id not in self.decisions:
            raise BoundaryError("NOT_FOUND", "판정이 없다")
        return copy.deepcopy(self.decisions[decision_id])

    def find_decisions(self, **kw):
        return list(self.decisions.values()), None

    def list_releases(self, **kw):
        return list(self.manifests.values()), None


# ---------------------------------------------------------------- I6 TraceIngest (ops-feedback 대역)
class FakeTraceIngest:
    """boundary_contracts.sdk.TraceIngest Protocol을 따르는 시험용 대역: 키 (세션, 턴)으로 멱등, 스키마 검증."""

    def __init__(self, validate: bool = True) -> None:
        self.received: dict[tuple, dict] = {}
        self.script: list = []         # 호출마다 앞에서 하나씩: None이면 정상, BoundaryError/예외면 던진다
        self.calls = 0
        self.validate = validate
        self._lock = threading.Lock()

    def send(self, envelope: Mapping) -> dict:
        import jsonschema
        import survey_schema
        from boundary_contracts import PIPELINE_SCHEMA_VERSION, validator
        with self._lock:
            self.calls += 1
            if self.script:
                step = self.script.pop(0)
                if step is not None:
                    raise step
            if self.validate:
                if list(validator("TraceEnvelope").iter_errors(envelope)):
                    raise BoundaryError("INVALID_RECORD", "봉투 위반")
                tv = jsonschema.Draft202012Validator(survey_schema.load_schema("TurnTrace", envelope["runtime_contract_version"]))
                if list(tv.iter_errors(envelope["trace"])):
                    raise BoundaryError("INVALID_RECORD", "TurnTrace 위반")
            key = (envelope["session_id"], envelope["turn_id"])
            status = "DUPLICATE" if key in self.received else "ACCEPTED"
            self.received.setdefault(key, copy.deepcopy(dict(envelope)))
            return {"pipeline_schema_version": PIPELINE_SCHEMA_VERSION, "session_id": key[0], "turn_id": key[1], "status": status,
                    "received_at": "2026-10-15T00:00:00Z"}
