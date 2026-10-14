"""실제 turn-engine + model-runtime의 FakeModelSession으로 Orchestrator를 돌리는 조립."""
from __future__ import annotations

import json
from typing import Optional

import survey_schema
from model_runtime import FakeBackend, FakeModelSession, fake_serving_config, serving_config_hash

from survey_service.orchestrator import Orchestrator
from survey_service.ports import LoadedRelease
from survey_service.release.registry import ReleaseRegistry
from survey_service.store.memory import MemorySessionStore

from .fakes import RecordingConsole, RecordingDispatcher
from .sample_bank import sample_bank


def item(no, qid, slot, value, text, intent="ANSWERED_INTENT", rk=""):
    return {"item_no": no, "target": {"question_id": qid, "repeat_key": rk, "slot_id": slot} if slot else None,
            "value": value, "source_text": text, "intent": intent, "progress_op": None, "guide_id": None}


def other(no, intent, text, value=None, target=None):
    return {"item_no": no, "target": target, "value": value, "source_text": text, "intent": intent,
            "progress_op": None, "guide_id": None}


class ScriptedModel:
    """parse는 큐에서 하나씩 꺼내 쓴다. generate는 지시의 동작 이름을 문장으로 돌려준다."""
    def __init__(self) -> None:
        self.parse_queue: list = []

    def responder(self, req):
        if req.role == "LORA_A":
            items = self.parse_queue.pop(0) if self.parse_queue else []
            return json.dumps({"items": items}, ensure_ascii=False)
        return json.dumps({"response_text": "응답입니다."}, ensure_ascii=False)


def make_session(model: ScriptedModel):
    backend = FakeBackend(model.responder)
    cfg = fake_serving_config(backend)
    return FakeModelSession(cfg, schema_dir=survey_schema.schema_dir(1), expected_serving_config_hash=serving_config_hash(cfg),
                            backend=backend)


class Rig:
    def __init__(self, store=None, bank=None, **orch_kw) -> None:
        self.model = ScriptedModel()
        self.bank = bank or sample_bank()
        self.store = store or MemorySessionStore()
        self.registry = ReleaseRegistry()
        self.release = LoadedRelease("rel-e2e-1", {}, self.bank, make_session(self.model))
        self.registry.register(self.release, activate=True)
        self.console = RecordingConsole()
        self.dispatcher = RecordingDispatcher()
        self.orch = Orchestrator(self.store, self.registry, staff_console=self.console, dispatcher=self.dispatcher, **orch_kw)
        self.n = 0

    def say(self, utterance=None, *, kind="UTTERANCE", items=None, sid="S1", turn_id=None, decision=None):
        self.n += 1
        tid = turn_id or f"t{self.n}"
        if items is not None:
            self.model.parse_queue.append(items)
        if decision:
            return self.orch.handle_turn(turn_id=tid, intervention_decision={"session_id": sid, "decision": decision})
        msg = {"session_id": sid, "kind": kind}
        if kind == "UTTERANCE":
            msg["utterance"] = utterance
        return self.orch.handle_turn(turn_id=tid, frontend_message=msg)

    def doc(self, sid="S1"):
        return self.store.load(sid)["state"]
