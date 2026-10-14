"""릴리스 로더: I4 ReleaseReader의 활성 포인터를 주기로 보고 번들을 적재한다.

- 포인터(`pointer_version`)가 바뀌었을 때만 읽는다. 같은 포인터 버전은 다시 시도하지 않는다.
- 새 세션부터 새 릴리스를 쓴다(적용 시점은 Orchestrator가 세션 생성 때 활성 릴리스에 묶는 것으로 지킨다).
- 적재할 수 없는 릴리스(지원하지 않는 계약 버전, 승인 기록 없음, 해시 불일치, 모델 거부)는 이전 릴리스를 유지하고 사유를 남긴다.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import jsonschema
from boundary_contracts import BoundaryError, canonical, validator

import survey_schema

from ..inference import InferenceLoadError, InferenceServer
from ..ports import LoadedRelease
from .registry import ReleaseRegistry

log = logging.getLogger("survey_service.release")


class LoadRejected(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class PollResult:
    action: str            # NO_POINTER | UNCHANGED | LOADED | ACTIVATED | REJECTED | RETRY_LATER
    release_id: Optional[str] = None
    code: Optional[str] = None


@dataclass
class LoaderStatus:
    last_pointer_version: Optional[int] = None
    active_release_id: Optional[str] = None
    rejections: list = field(default_factory=list)       # {pointer_version, release_id, code, detail}
    warnings: list = field(default_factory=list)
    last_poll_error: Optional[str] = None


class ReleaseLoader:
    def __init__(self, reader, registry: ReleaseRegistry, inference: InferenceServer, *, workdir: Path,
                 supported_versions: Optional[Iterable[int]] = None, installed_versions: Optional[dict] = None,
                 keep_releases: int = 2, open_release_ids: Callable[[], set] = lambda: set()) -> None:
        self.reader, self.registry, self.inference = reader, registry, inference
        self.workdir = Path(workdir)
        self.supported = set(supported_versions) if supported_versions is not None else {survey_schema.current_version()}
        self.installed = installed_versions or {}
        self.keep = keep_releases
        self.open_release_ids = open_release_ids
        self.status = LoaderStatus()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 폴링
    def poll_once(self) -> PollResult:
        with self._lock:
            try:
                ptr = self.reader.get_active_pointer()
                self.status.last_poll_error = None
            except BoundaryError as e:
                self.status.last_poll_error = e.code
                log.warning("활성 포인터를 읽지 못했다 code=%s", e.code)
                return PollResult("RETRY_LATER", code=e.code)
            if ptr is None:
                return PollResult("NO_POINTER")
            if ptr["pointer_version"] == self.status.last_pointer_version:
                return PollResult("UNCHANGED", ptr["release_id"])
            rid = ptr["release_id"]
            try:
                already = rid in self.registry.loaded_ids()
                if not already:
                    rel = self._load(rid)
                    self.registry.register(rel)
                self.registry.activate(rid)
            except LoadRejected as e:
                return self._reject(ptr, e)
            except BoundaryError as e:
                if e.retryable:                      # 일시 장애는 다음 주기에 다시
                    self.status.last_poll_error = e.code
                    log.warning("릴리스를 읽지 못했다 release=%s code=%s", rid, e.code)
                    return PollResult("RETRY_LATER", rid, e.code)
                return self._reject(ptr, LoadRejected(e.code, e.message))
            self.status.last_pointer_version = ptr["pointer_version"]
            self.status.active_release_id = rid
            self.registry.gc(self.open_release_ids(), self.keep)
            log.info("릴리스 적용 release=%s pointer_version=%s", rid, ptr["pointer_version"])
            return PollResult("ACTIVATED" if already else "LOADED", rid)

    def _reject(self, ptr: dict, e: LoadRejected) -> PollResult:
        self.status.last_pointer_version = ptr["pointer_version"]    # 같은 포인터는 다시 시도하지 않는다
        self.status.rejections.append({"pointer_version": ptr["pointer_version"], "release_id": ptr["release_id"],
                                       "code": e.code, "detail": e.detail})
        del self.status.rejections[:-20]
        log.error("릴리스 적재 거부(이전 릴리스 유지) release=%s code=%s", ptr["release_id"], e.code)
        return PollResult("REJECTED", ptr["release_id"], e.code)

    # ------------------------------------------------------------------ 적재
    def resolve(self, release_id: str) -> LoadedRelease:
        """서비스를 다시 띄운 뒤 진행 중 세션이 가리키는 릴리스를 다시 적재한다(ReleaseRegistry의 resolver)."""
        with self._lock:
            try:
                return self._load(release_id)
            except LoadRejected as e:
                from ..errors import ReleaseUnavailable
                raise ReleaseUnavailable(e.code) from e

    def _load(self, rid: str) -> LoadedRelease:
        manifest = self.reader.get_release(rid)
        self._check_manifest(manifest, rid)
        self._check_decision(manifest)
        dest = self.workdir / rid
        dest.mkdir(parents=True, exist_ok=True)
        bundle = self.reader.load_bundle(rid, dest=dest)      # 해시 검증, PASS가 아니면 FAILED_PRECONDITION(BoundaryError)
        bank = bundle.question_bank
        self._check_bank(manifest, bank)
        warnings = self._version_warnings(manifest)
        try:
            session = self.inference.load(manifest, bundle.adapters, contract_version=manifest["runtime_contract_version"])
        except InferenceLoadError as e:
            raise LoadRejected(e.code, str(e)) from e
        for w in warnings:
            log.warning("릴리스 경고 release=%s %s", rid, w)
        self.status.warnings = (self.status.warnings + [{"release_id": rid, "warning": w} for w in warnings])[-20:]
        return LoadedRelease(rid, manifest, bank, session, warnings)

    def _check_manifest(self, manifest: dict, rid: str) -> None:
        errs = list(validator("ReleaseManifest").iter_errors(manifest))
        if errs:
            raise LoadRejected("INVALID_RECORD", "ReleaseManifest가 스키마를 어긴다")
        if manifest["release_id"] != rid:
            raise LoadRejected("INVALID_RECORD", "포인터와 매니페스트의 release_id가 다르다")
        if manifest["runtime_contract_version"] not in self.supported:
            raise LoadRejected("UNSUPPORTED_VERSION", f"지원하지 않는 runtime_contract_version {manifest['runtime_contract_version']}")
        if canonical.content_hash(manifest) != manifest["content_hash"]:
            raise LoadRejected("INTEGRITY_ERROR", "매니페스트 content_hash가 맞지 않는다")

    def _check_decision(self, manifest: dict) -> None:
        """게이트가 승인한 묶음만 적재한다: 승인(PASS) 기록이 이 조합을 가리켜야 한다."""
        try:
            dec = self.reader.get_decision(manifest["gate_decision_id"])
        except BoundaryError as e:
            if e.retryable:
                raise
            raise LoadRejected("NO_APPROVAL", f"승인 기록을 읽을 수 없다({e.code})") from e
        subject = {k: manifest[k] for k in ("question_set", "lora_a", "lora_b")}
        if dec.get("result") != "PASS":
            raise LoadRejected("NO_APPROVAL", "게이트 판정이 PASS가 아니다")
        if dec.get("subject") != subject or dec.get("runtime_contract_version") != manifest["runtime_contract_version"]:
            raise LoadRejected("NO_APPROVAL", "승인 기록이 이 조합을 가리키지 않는다")

    def _check_bank(self, manifest: dict, bank: dict) -> None:
        ver = manifest["runtime_contract_version"]
        v = jsonschema.Draft202012Validator(survey_schema.load_schema("QuestionBank", ver))
        if list(v.iter_errors(bank)):
            raise LoadRejected("INVALID_RECORD", "문항 세트가 QuestionBank 스키마를 어긴다")
        if canonical.sha256_hash(bank) != manifest["question_set"]["content_hash"]:
            raise LoadRejected("INTEGRITY_ERROR", "문항 세트 해시가 매니페스트와 다르다")
        if str(bank["version"]) != str(manifest["question_set"]["version"]):
            raise LoadRejected("INVALID_RECORD", "문항 세트 버전이 ArtifactRef와 다르다")

    def _version_warnings(self, manifest: dict) -> list:
        out = []
        for key, name in (("turn_engine_version", "turn-engine"), ("model_runtime_version", "model-runtime")):
            have = self.installed.get(key)
            if have and manifest[key] != have:
                out.append(f"{name} 버전이 다르다(릴리스 {manifest[key]}, 설치 {have}). 정책은 CR-005 대기")
        return out
