"""실제 turn-engine(validate_extraction) + model-runtime의 FakeModelSession으로 Orchestrator를 돌리는 조립(D74 구조).

LoRA-A가 낼 ActionExtraction은 시험이 큐에 넣는다. LoRA-B는 고정 문장을 낸다. 서비스가 모델에 보낸 요청은 `session.extract_requests`,
`session.respond_requests`에 남는다."""
from __future__ import annotations

import copy
import json
from typing import Optional

import survey_schema
from model_runtime import FakeBackend, FakeModelSession, fake_serving_config, serving_config_hash

from survey_service.inference import LocalModelSession
from survey_service.orchestrator import Orchestrator
from survey_service.ports import LoadedRelease
from survey_service.release.registry import ReleaseRegistry
from survey_service.store.memory import MemorySessionStore

from .fakes import RecordingConsole, RecordingDispatcher
from .sample_bank import sample_bank


def val(key, value, status="ANSWERED", precision="exact"):
    """values 항목 하나. save는 status에서 정해진다(ANSWERED·CORRECTION만 true), 값 없는 상태는 value가 null이다."""
    if status in ("MISSING", "UNCLEAR", "UNKNOWN", "REJECTED"):
        value, precision = None, "unspecified"
    return {"key": key, "value": value, "precision": precision, "status": status, "save": status in ("ANSWERED", "CORRECTION")}


def ext(action, next_item, *values, reason=None, targets=None, note=None):
    out = {"action": action, "next_item": next_item, "values": list(values)}
    if reason:
        out["reason"] = reason
    if targets:
        out["targets"] = list(targets)
    if note:
        out["note"] = note
    return out


class ScriptedModel:
    """extract는 큐에서 하나씩 꺼내 쓴다(dict는 JSON으로, 문자열은 그대로: 깨진 출력 시험). respond는 고정 문장."""
    def __init__(self) -> None:
        self.queue: list = []
        self.response = "응답입니다."

    def responder(self, req):
        if req.role == "LORA_A":
            item = self.queue.pop(0) if self.queue else ext("NEXT", "END")
            return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return self.response


class RecordingSession(LocalModelSession):
    def __init__(self, session) -> None:
        super().__init__(session)
        self.extract_requests: list = []
        self.respond_requests: list = []

    def extract(self, request):
        self.extract_requests.append(copy.deepcopy(dict(request)))
        return super().extract(request)

    def respond(self, request):
        self.respond_requests.append(copy.deepcopy(dict(request)))
        return super().respond(request)


def make_session(model: ScriptedModel) -> RecordingSession:
    backend = FakeBackend(model.responder)
    cfg = fake_serving_config(backend)
    return RecordingSession(FakeModelSession({"LORA_A": cfg, "LORA_B": cfg}, schema_dir=survey_schema.schema_dir(1),
                                             expected_serving_config_hashes={r: serving_config_hash(cfg) for r in ("LORA_A", "LORA_B")},
                                             backend=backend))


class Rig:
    def __init__(self, store=None, bank=None, risk: bool = False, **orch_kw) -> None:
        if risk:
            orch_kw["risk_source"] = ScriptedRisk()
        self.model = ScriptedModel()
        self.bank = bank or sample_bank()
        self.store = store or MemorySessionStore()
        self.registry = ReleaseRegistry()
        self.session = make_session(self.model)
        self.release = LoadedRelease("rel-e2e-1", {}, self.bank, self.session)
        self.registry.register(self.release, activate=True)
        self.console = RecordingConsole()
        self.dispatcher = RecordingDispatcher()
        self.orch = Orchestrator(self.store, self.registry, staff_console=self.console, dispatcher=self.dispatcher, **orch_kw)
        self.n = 0

    def say(self, utterance=None, *, kind="UTTERANCE", out=None, sid="S1", turn_id=None, decision=None):
        self.n += 1
        tid = turn_id or f"t{self.n}"
        if out is not None:
            self.model.queue.append(out)
        if decision:
            return self.orch.handle_turn(turn_id=tid, intervention_decision={"session_id": sid, "decision": decision})
        msg = {"session_id": sid, "kind": kind}
        if kind == "UTTERANCE":
            msg["utterance"] = utterance
        return self.orch.handle_turn(turn_id=tid, frontend_message=msg)

    def start(self, sid="S1", turn_id=None):
        return self.say(kind="SESSION_START", sid=sid, turn_id=turn_id)

    def doc(self, sid="S1"):
        return self.store.load(sid)["state"]


class ScriptedRisk:
    """RiskSource 시험용 대역. v2의 A 출력에는 위험 신호가 없어 실제 입력원은 없다(CR-012). 직원 알림 경로가 살아 있는지 보려고 꽂는다."""
    def __init__(self, phrase="숨이 안 쉬어져요", kind: str = "RISK") -> None:
        self.phrases = (phrase,) if isinstance(phrase, str) else tuple(phrase)
        self.kind = kind
        self.calls = 0

    def check(self, *, extract_input, extract_output, utterance):
        self.calls += 1
        hit = next((p for p in self.phrases if p in utterance), None)
        if hit is None:
            return None
        if self.kind == "RISK":
            return {"kind": "RISK", "reasons": [{"kind": "RISK_SIGNAL", "evidence": [hit]}]}
        return {"kind": self.kind, "reasons": []}


# 시험에서 자주 쓰는 A 출력
ANSWER_OUT = ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", False), val("Q1.D01.on_medication", False))       # Q1.D01을 답하고 다음 문항으로
RISK_OUT = ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC")                                                       # 위험 입력원이 알림을 내는 턴의 A 출력(내용은 상관없다)
