"""릴리스 로더: I4 ReleaseReader의 활성 포인터를 주기로 보고 번들을 적재한다.

- 포인터(`pointer_version`)가 바뀌었을 때만 읽는다. 같은 포인터 버전은 다시 시도하지 않는다.
- 새 세션부터 새 릴리스를 쓴다(적용 시점은 Orchestrator가 세션 생성 때 활성 릴리스에 묶는 것으로 지킨다).
- 적재할 수 없는 릴리스(지원하지 않는 계약 버전, 승인 기록 없음, 해시 불일치, 모델 거부)는 이전 릴리스를 유지하고 사유를 남긴다.
  **결정적 거부**(412·422·404 등)는 그 포인터를 영구 거부(rejections)로 확정하고 다시 시도하지 않는다.
- **일시적 실패**(I4 불능, 추론 서버가 아직 뜨는 중이라 연결 실패·503·타임아웃·적재 대기 시간 초과)는 포인터를 거부로 확정하지 않는다.
  같은 포인터를 백오프(기본 5초 시작, 최대 60초)로 계속 다시 시도하고(`status.retry`), 포인터가 새 버전으로 바뀌면 새 포인터를 먼저 본다.
  상한(`give_up_seconds`, 기본 없음)을 넘기면 RETRY_EXHAUSTED로 거부를 확정한다. 서비스는 그동안에도 살아 있다.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
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


# 서비스가 직접 환자 화면에 내보내는 고정 문장(graph.py의 _fixed 호출). 문항 세트에 없으면 적재를 거부한다.
REQUIRED_FIXED_SENTENCES = ("RISK_NOTICE", "CALL_STAFF", "RESUME", "SURVEY_STOPPED")


class LoadRejected(Exception):
    def __init__(self, code: str, detail: str = "", *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable      # True면 일시적이라 나중에 다시 시도한다(영구 거부가 아니다)


@dataclass
class PollResult:
    action: str            # NO_POINTER | UNCHANGED | LOADED | ACTIVATED | REJECTED | RETRY_LATER | WAITING
    release_id: Optional[str] = None
    code: Optional[str] = None


@dataclass
class LoaderStatus:
    last_pointer_version: Optional[int] = None
    active_release_id: Optional[str] = None
    rejections: list = field(default_factory=list)       # {pointer_version, release_id, code, detail}
    warnings: list = field(default_factory=list)
    last_poll_error: Optional[str] = None
    unavailable_releases: list = field(default_factory=list)   # 진행 중 세션이 쓰는데 적재하지 못한 릴리스
    # 일시적 실패로 재시도 중인 포인터: {pointer_version, release_id, attempts, first_failure_at, last_attempt_at, next_attempt_at, last_error}
    retry: Optional[dict] = None


class ReleaseLoader:
    def __init__(self, reader, registry: ReleaseRegistry, inference: InferenceServer, *, workdir: Path,
                 supported_versions: Optional[Iterable[int]] = None, installed_versions: Optional[dict] = None,
                 keep_releases: int = 2, open_release_ids: Callable[[], set] = lambda: set(),
                 retry_base_seconds: float = 5.0, retry_max_seconds: float = 60.0, give_up_seconds: float = 0.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.reader, self.registry, self.inference = reader, registry, inference
        self.workdir = Path(workdir)
        self.supported = set(supported_versions) if supported_versions is not None else {survey_schema.current_version()}
        self.installed = installed_versions or {}
        self.keep = keep_releases
        self.open_release_ids = open_release_ids
        self.status = LoaderStatus()
        self._lock = threading.Lock()
        self.retry_base, self.retry_max, self.give_up, self.clock = retry_base_seconds, retry_max_seconds, give_up_seconds, clock

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
            rs = self.status.retry
            if rs is not None and rs["pointer_version"] != ptr["pointer_version"]:
                log.info("재시도 중이던 포인터 대신 새 포인터를 본다 old=%s new=%s", rs["pointer_version"], ptr["pointer_version"])
                self.status.retry = rs = None                      # 새 포인터가 우선이다
            if rs is not None and self.clock() < rs["_next_ts"]:
                return PollResult("WAITING", rid, rs["last_error"])   # 백오프 중: 다음 시도 시각 전에는 부르지 않는다
            try:
                already = rid in self.registry.loaded_ids()
                if not already:
                    rel = self._load(rid)
                    self.registry.register(rel)
                self.registry.activate(rid)
            except LoadRejected as e:
                if e.retryable:
                    return self._retry(ptr, e.code, e.detail)
                return self._reject(ptr, e)
            except BoundaryError as e:
                if e.retryable:                      # 일시 장애는 포인터를 확정하지 않고 백오프로 다시
                    return self._retry(ptr, e.code, e.message)
                return self._reject(ptr, LoadRejected(e.code, e.message))
            self.status.retry = None
            self.status.last_pointer_version = ptr["pointer_version"]
            self.status.active_release_id = rid
            self.registry.gc(self.open_release_ids(), self.keep)
            log.info("릴리스 적용 release=%s pointer_version=%s", rid, ptr["pointer_version"])
            return PollResult("ACTIVATED" if already else "LOADED", rid)

    @staticmethod
    def _iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _retry(self, ptr: dict, code: str, detail: str) -> PollResult:
        """일시적 실패: 포인터를 거부로 확정하지 않고 백오프 뒤 같은 포인터를 다시 시도한다."""
        now = self.clock()
        rs = self.status.retry
        if rs is None:
            rs = self.status.retry = {"pointer_version": ptr["pointer_version"], "release_id": ptr["release_id"], "attempts": 0,
                                      "first_failure_at": self._iso(now), "_first_ts": now}
        rs["attempts"] += 1
        delay = min(self.retry_max, self.retry_base * 2 ** (rs["attempts"] - 1))
        rs.update(last_attempt_at=self._iso(now), next_attempt_at=self._iso(now + delay), _next_ts=now + delay,
                  last_error=f"{code}: {detail}"[:200])
        self.status.last_poll_error = None
        if self.give_up > 0 and now - rs["_first_ts"] >= self.give_up:      # 상한(기본 없음)
            self.status.retry = None
            return self._reject(ptr, LoadRejected("RETRY_EXHAUSTED", f"{rs['attempts']}번 시도했으나 계속 일시적으로 실패했다 ({code})"))
        log.warning("릴리스 적재를 나중에 다시 시도한다 release=%s attempts=%s code=%s next_in=%.0fs", ptr["release_id"], rs["attempts"], code, delay)
        return PollResult("RETRY_LATER", ptr["release_id"], code)

    def retry_info(self) -> Optional[dict]:
        """/readyz용: 재시도 중이면 공개 필드만(내부 시각 제외)."""
        rs = self.status.retry
        return None if rs is None else {k: v for k, v in rs.items() if not k.startswith("_")}

    def _reject(self, ptr: dict, e: LoadRejected) -> PollResult:
        self.status.last_pointer_version = ptr["pointer_version"]    # 같은 포인터는 다시 시도하지 않는다
        self.status.rejections.append({"pointer_version": ptr["pointer_version"], "release_id": ptr["release_id"],
                                       "code": e.code, "detail": e.detail})
        del self.status.rejections[:-20]
        log.error("릴리스 적재 거부(이전 릴리스 유지) release=%s code=%s", ptr["release_id"], e.code)
        return PollResult("REJECTED", ptr["release_id"], e.code)

    # ------------------------------------------------------------------ 적재
    def ensure_loaded(self, release_ids: Iterable[str]) -> list[str]:
        """진행 중 세션이 묶인 릴리스를 적재해 둔다(서비스를 다시 띄운 뒤). 턴 밖에서 부른다. 적재하지 못한 ID를 돌려준다."""
        missing = []
        for rid in release_ids:
            if rid in self.registry.loaded_ids():
                continue
            with self._lock:
                try:
                    self.registry.register(self._load(rid))
                except LoadRejected as e:
                    log.error("진행 중 세션의 릴리스를 적재하지 못했다 release=%s code=%s", rid, e.code)
                    missing.append(rid)
                except BoundaryError as e:
                    log.error("진행 중 세션의 릴리스를 읽지 못했다 release=%s code=%s", rid, e.code)
                    missing.append(rid)
        self.status.unavailable_releases = missing
        return missing

    def _load(self, rid: str) -> LoadedRelease:
        manifest = self.reader.get_release(rid)
        self._check_manifest(manifest, rid)
        self._check_decision(manifest)
        # 클라이언트가 dest/<릴리스 ID>에 풀고 모든 해시를 검증한다. PASS가 아니면 FAILED_PRECONDITION(BoundaryError).
        # 어댑터와 질문지는 번들(또는 문항 세트 호출)이 돌려준 경로·내용을 그대로 쓴다. 로컬 사본은 재사용하지 않고 매번 새로 받아 검증한 뒤 교체한다(손상된 사본은 덮어쓴다).
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
            if getattr(self.inference, "needs_adapters", True):      # 서비스 안에서 모델을 올리는 백엔드만 어댑터를 쓴다
                bundle = self.reader.load_bundle(rid, dest=self.workdir)
                bank, adapters = bundle.question_bank, bundle.adapters
            else:                                                    # 원격 추론: 어댑터는 model-runtime이 따로 받는다. 문항 세트만 받는다(I4, 계약 2.7.0, ADR-0040)
                bank, adapters = self.reader.get_release_question_bank(rid)["question_bank"], {}
        except OSError as e:               # 볼륨을 쓸 수 없다(읽기 전용, 가득 참, 권한): 폴링 스레드를 죽이지 않고 일시 실패로 다시 시도한다
            raise LoadRejected("STORAGE_UNAVAILABLE", f"작업 폴더를 쓸 수 없다: {type(e).__name__}", retryable=True) from None
        self._check_bank(manifest, bank)
        warnings = self._check_library_versions(manifest)
        try:
            session = self.inference.load(manifest, adapters, contract_version=manifest["runtime_contract_version"])
        except InferenceLoadError as e:
            raise LoadRejected(e.code, str(e), retryable=e.retryable) from e
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
        have = {f["key"] for f in bank["session_settings"]["fixed_sentences"] if isinstance(f.get("sentence"), str) and f["sentence"].strip()}
        missing = sorted(set(REQUIRED_FIXED_SENTENCES) - have)
        if missing:      # 위험 정지 안내·직원 호출·재개·종료 안내가 비면 환자 화면이 비므로 적재 단계에서 거부한다(감사 I3)
            raise LoadRejected("MISSING_FIXED_SENTENCE", "필수 고정 문장이 없거나 비어 있다: " + ", ".join(missing))

    def _check_library_versions(self, manifest: dict) -> list:
        """게이트가 승인한 동작과 서비스 동작이 달라지지 않게(I4): turn-engine·model-runtime의 MAJOR.MINOR가 설치본과 다르면 거부,
        PATCH 차이는 경고만 하고 적재한다(헤드 결정, CR-005)."""
        out = []
        for key, name in (("turn_engine_version", "turn-engine"), ("model_runtime_version", "model-runtime")):
            have = self.installed.get(key)
            if not have:
                continue
            want = manifest[key]
            hm, wm = _major_minor(have), _major_minor(want)
            if hm is None or wm is None:
                raise LoadRejected("VERSION_MISMATCH", f"{name} 버전을 해석할 수 없다(릴리스 {want}, 설치 {have})")
            if hm != wm:
                raise LoadRejected("VERSION_MISMATCH", f"{name} MAJOR.MINOR가 다르다(릴리스 {want}, 설치 {have})")
            if want != have:
                out.append(f"{name} PATCH가 다르다(릴리스 {want}, 설치 {have})")
        # 폐기 예정 프롬프트 템플릿(prompt-3 등)은 거부하지 않고 경고만 한다(model-runtime이 허용, D77). 기준은 설치된 model-runtime의 목록.
        try:
            from model_runtime.prompts import DEPRECATED_TEMPLATE_VERSIONS, DEFAULT_TEMPLATE_VERSION
        except ImportError:   # 0.6.0 이하 설치본에는 목록이 없다
            DEPRECATED_TEMPLATE_VERSIONS, DEFAULT_TEMPLATE_VERSION = frozenset(), ""
        for role, ad in sorted((manifest.get("adapters") or {}).items()):
            tv = (ad.get("serving_config") or {}).get("prompt_template_version")
            if tv in DEPRECATED_TEMPLATE_VERSIONS:
                out.append(f"{role}의 prompt_template_version {tv}는 폐기 예정이다(기본 {DEFAULT_TEMPLATE_VERSION})")
        return out


def _major_minor(v: str):
    m = re.match(r"^(\d+)\.(\d+)(?:\.\d+)?", str(v))
    return (int(m.group(1)), int(m.group(2))) if m else None
