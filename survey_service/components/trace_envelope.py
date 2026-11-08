"""Trace 봉투 컴포넌트: TurnTrace를 TraceEnvelope(계약)에 담는다. 입력 TraceEnvelopeSource -> 출력 TraceEnvelope. 바깥 ID는 `Id` 형식으로 바꾼다(CONVENTIONS 10절)."""
from __future__ import annotations


from datetime import datetime, timezone
from typing import Callable

# 왜 계약 함수인가: 바깥 ID 를 봉투 Id 로 바꾸는 규칙(CONVENTIONS 116행)의 정본 구현은 계약 패키지다(5.0.0). 영역마다 구현하면 끝 줄바꿈·길이 경계가 갈려 수집이 거부한 일이 있었다(D117).
from boundary_contracts import PIPELINE_SCHEMA_VERSION, envelope_id


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


