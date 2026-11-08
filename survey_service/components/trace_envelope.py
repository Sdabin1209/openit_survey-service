"""Trace 봉투 컴포넌트: TurnTrace를 TraceEnvelope(계약)에 담는다. 입력 TraceEnvelopeSource -> 출력 TraceEnvelope. 바깥 ID는 `Id` 형식으로 바꾼다(CONVENTIONS 10절)."""
from __future__ import annotations


import hashlib
import re
from datetime import datetime, timezone
from typing import Callable

from boundary_contracts import PIPELINE_SCHEMA_VERSION, load_schema

# 왜 계약에서 읽나: Id 의 pattern 은 끝 줄바꿈을 막는 (?![\s\S]) 까지 계약의 것이다. 코드에 사본을 두면 빠뜨려 "abc\n" 같은 원본에서 수집 쪽(ops-feedback)과 결과가 갈린다(구조 규칙: 계약은 패키지에서 읽는다, CONVENTIONS 116행).
_ID_DEF = load_schema("_common")["$defs"]["Id"]
_ID = re.compile(_ID_DEF["pattern"])
_ID_MIN, _ID_MAX = _ID_DEF.get("minLength", 1), _ID_DEF["maxLength"]


def envelope_id(raw: str) -> str:
    """바깥(Frontend)이 정한 세션 ID·턴 ID를 봉투의 Id 형식으로. 형식에 맞으면 그대로, 아니면 'x-' + 원본 UTF-8 SHA-256 앞 32자
    (소문자 16진). 결정적이라 멱등이다. 원본은 trace 본문에 있다(CONVENTIONS 10절)."""
    if _ID_MIN <= len(raw) <= _ID_MAX and _ID.match(raw):
        return raw
    return "x-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OutboxTraceSink:
    """Orchestrator의 TraceSink 구현: TurnTrace를 TraceEnvelope에 담아 outbox에 넣는다."""
    def __init__(self, outbox, clock: Callable[[], float]) -> None:
        self.outbox, self.clock = outbox, clock

    def envelope(self, release_id: str, trace: dict, question_set_id: str | None = None) -> dict:
        now = self.clock()
        env = {"pipeline_schema_version": PIPELINE_SCHEMA_VERSION, "release_id": release_id,
               "runtime_contract_version": trace["contract_version"], "session_id": envelope_id(trace["session_id"]),
               "turn_id": envelope_id(trace["turn_id"]), "sent_at": _iso(now), "trace": trace}
        if question_set_id is not None:
            env["question_set_id"] = question_set_id      # 릴리스에 문진표가 여럿이라 release_id만으로는 이 세션의 문진표를 모른다(계약 3.3.0 TraceEnvelope.question_set_id)
        return env

    def submit(self, release_id: str, trace: dict, question_set_id: str | None = None) -> None:
        """세션 저장소와 outbox가 다른 곳이면(시험, 메모리 저장소) 이 경로로 따로 넣는다. 같은 DB면 Orchestrator가 저장소 트랜잭션에 쓴다."""
        self.outbox.enqueue(self.envelope(release_id, trace, question_set_id), self.clock())


