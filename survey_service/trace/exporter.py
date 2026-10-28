"""Trace 내보내기: outbox의 봉투를 I6 TraceIngest로 보낸다.

- 최소 1회 전달: 보냈다는 확인(ACCEPTED 또는 DUPLICATE)을 받기 전에는 지우지 않는다. 수집이 (세션, 턴)으로 멱등 처리한다.
- UNAVAILABLE(retryable)일 때만 재시도한다. 그 밖의 BoundaryError는 재시도하지 않고 DEAD로 옮긴다.
- 턴 처리 경로와 분리: enqueue는 Orchestrator가, 전송은 이 스레드가 한다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import jsonschema
from boundary_contracts import PIPELINE_SCHEMA_VERSION, BoundaryError, validator

import survey_schema

from .outbox import Outbox

log = logging.getLogger("survey_service.trace")


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def envelope_id(raw: str) -> str:
    """바깥(Frontend)이 정한 세션 ID·턴 ID를 봉투의 Id 형식으로. 형식에 맞으면 그대로, 아니면 'x-' + 원본 UTF-8 SHA-256 앞 32자
    (소문자 16진). 결정적이라 멱등이다. 원본은 trace 본문에 있다(CONVENTIONS 10절)."""
    if len(raw) <= 128 and _ID.match(raw):
        return raw
    return "x-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OutboxTraceSink:
    """Orchestrator의 TraceSink 구현: TurnTrace를 TraceEnvelope에 담아 outbox에 넣는다."""
    def __init__(self, outbox: Outbox, clock: Callable[[], float] = time.time) -> None:
        self.outbox, self.clock = outbox, clock

    def submit(self, release_id: str, trace: dict) -> None:
        now = self.clock()
        envelope = {"pipeline_schema_version": PIPELINE_SCHEMA_VERSION, "release_id": release_id,
                    "runtime_contract_version": trace["contract_version"], "session_id": envelope_id(trace["session_id"]),
                    "turn_id": envelope_id(trace["turn_id"]), "sent_at": _iso(now), "trace": trace}
        self.outbox.enqueue(envelope, now)


@dataclass
class FlushResult:
    sent: int = 0
    duplicate: int = 0
    retried: int = 0
    dead: int = 0
    paused: bool = False


class TraceExporter:
    def __init__(self, outbox: Outbox, ingest, *, base_delay: float = 1.0, max_delay: float = 60.0, batch: int = 50,
                 validate: bool = True, clock: Callable[[], float] = time.time) -> None:
        self.outbox, self.ingest = outbox, ingest
        self.base_delay, self.max_delay, self.batch = base_delay, max_delay, batch
        self.validate = validate
        self.clock = clock
        self.consecutive_unavailable = 0
        self.pause_until = 0.0
        self.last_error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._env_validator = None
        self._trace_validators: dict[int, jsonschema.Draft202012Validator] = {}

    # ------------------------------------------------------------------ 검증(전송 전, 턴 경로 밖)
    def _local_errors(self, env: dict) -> Optional[str]:
        if self._env_validator is None:
            self._env_validator = validator("TraceEnvelope")
        errs = list(self._env_validator.iter_errors(env))
        if errs:
            return f"봉투가 TraceEnvelope 스키마를 어긴다: {'/'.join(map(str, errs[0].absolute_path))} {errs[0].validator}"
        ver = env["runtime_contract_version"]
        v = self._trace_validators.get(ver)
        if v is None:
            try:
                v = jsonschema.Draft202012Validator(survey_schema.load_schema("TurnTrace", ver))
            except KeyError:
                return f"이 서비스에 없는 문진 스키마 정수 버전 {ver}"
            self._trace_validators[ver] = v
        errs = list(v.iter_errors(env["trace"]))
        if errs:
            return f"trace가 TurnTrace 스키마를 어긴다: {'/'.join(map(str, errs[0].absolute_path))} {errs[0].validator}"
        return None

    # ------------------------------------------------------------------ 한 번 비우기
    def flush_once(self) -> FlushResult:
        res = FlushResult()
        now = self.clock()
        if now < self.pause_until:
            res.paused = True
            return res
        for row in self.outbox.due(now, self.batch):
            if self.validate:
                bad = self._local_errors(row.envelope)
                if bad:
                    self.outbox.mark_dead(row.id, now, "LOCAL_INVALID: " + bad)
                    log.error("전송하지 않을 Trace(스키마 위반) session=%s turn=%s", row.session_id, row.turn_id)
                    res.dead += 1
                    continue
            try:
                r = self.ingest.send(row.envelope)
            except BoundaryError as e:
                if e.retryable:                  # 왜: 계약 I6(INTERFACES.md)·CONVENTIONS 4절 "UNAVAILABLE일 때만 재시도". INTEGRITY_ERROR는 retryable=false(CONVENTIONS 3절)
                    self.consecutive_unavailable += 1
                    delay = min(self.max_delay, self.base_delay * 2 ** (self.consecutive_unavailable - 1))
                    self.pause_until = now + delay
                    self.last_error = e.code
                    self.outbox.mark_failed(row.id, now, e.code)
                    res.retried += 1
                    res.paused = True
                    return res                       # 수집이 죽어 있으면 나머지도 같다: 다음 주기까지 쉰다
                # 왜: 재시도하지 않는 실패(INTEGRITY_ERROR 포함)는 지우지 않고 DEAD(격리)로 남긴다. dead_rows/dead_by_code(/readyz)로 사람이 보고, 보존 기간(retention)이 지나야 지워진다
                self.outbox.mark_dead(row.id, now, f"{e.code}: {e.message}")
                self.last_error = e.code
                log.error("재시도하지 않는 전송 실패 code=%s session=%s turn=%s", e.code, row.session_id, row.turn_id)
                res.dead += 1
                continue
            except Exception as e:                    # 왜: 네트워크 실패는 클라이언트가 UNAVAILABLE로 바꾼다(_http.py). 날것의 예외는 UNAVAILABLE이 아니므로 재시도하지 않고 버그 신호로 격리한다
                err = f"UNEXPECTED: {type(e).__name__}"
                self.last_error = err
                self.outbox.mark_dead(row.id, now, err)
                log.error("예상 밖 전송 예외 error=%s session=%s turn=%s", type(e).__name__, row.session_id, row.turn_id)
                res.dead += 1
                continue
            self.consecutive_unavailable = 0
            self.outbox.mark_sent(row.id, now)
            if r.get("status") == "DUPLICATE":
                res.duplicate += 1
            else:
                res.sent += 1
        return res

    # ------------------------------------------------------------------ 스레드
    def start(self, interval: float = 1.0) -> None:
        if self._thread:
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.flush_once()
                except Exception as e:                # 스레드가 죽으면 안 된다
                    log.error("Trace 전송 주기 실패 error=%s", type(e).__name__)
        self._thread = threading.Thread(target=loop, name="trace-exporter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def stats(self) -> dict:
        now = self.clock()
        c = self.outbox.counts()
        return {"pending": c["PENDING"], "sent": c["SENT"], "dead": c["DEAD"],
                "dropped_full": getattr(self.outbox, "dropped_full", 0),
                "dead_by_code": self.outbox.dead_by_code() if c["DEAD"] else {},
                "oldest_pending_age_seconds": self.outbox.oldest_pending_age(now),
                "consecutive_unavailable": self.consecutive_unavailable, "last_error": self.last_error}
