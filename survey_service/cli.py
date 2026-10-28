"""개발용 하네스(공개 API가 아니다, O06 대기). 한 턴을 처리하고 결과를 JSON으로 찍는다.

문항 세트 파일과 SQLite 파일을 주면 여러 번 부르는 동안 세션이 이어진다. 모델은 FakeModelSession이고, LoRA-A가 낼 ActionExtraction은 사람이
`--extraction`으로 직접 준다(의미 규칙은 서버가 보증한다, ADR 0010). 응답 문장은 가짜다.

예: python -m survey_service.cli --bank bank.json --db /tmp/s.db turn --session S1 --start
    python -m survey_service.cli --bank bank.json --db /tmp/s.db turn --session S1 --utterance "안 피움" --extraction '{...}'
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

import model_runtime
import survey_schema

from .orchestrator import Orchestrator
from .inference import LocalModelSession
from .ports import LoadedRelease
from .release.registry import ReleaseRegistry
from .store.sqlite import SqliteSessionStore


class _Console:
    def deliver(self, console_input):
        print("[직원 콘솔]", json.dumps(console_input, ensure_ascii=False), file=sys.stderr)


class _Dispatcher:
    def dispatch(self, dispatch_input):
        print("[결과 전달]", dispatch_input["session_status"], file=sys.stderr)
        return {"dispatch_record": {"succeeded": True}}


def build(bank: dict, db: str, extraction: dict):
    def responder(req):
        if req.role == "LORA_A":
            return json.dumps(extraction, ensure_ascii=False)
        return "(가짜 응답)"

    backend = model_runtime.FakeBackend(responder)
    cfg = model_runtime.fake_serving_config(backend)
    session = model_runtime.FakeModelSession({"LORA_A": cfg, "LORA_B": cfg}, schema_dir=survey_schema.schema_dir(survey_schema.current_version()),
                                             expected_serving_config_hashes={r: model_runtime.serving_config_hash(cfg) for r in ("LORA_A", "LORA_B")},
                                             backend=backend)
    reg = ReleaseRegistry()
    reg.register(LoadedRelease("rel-dev", {}, bank, LocalModelSession(session)), activate=True)
    return Orchestrator(SqliteSessionStore(db), reg, staff_console=_Console(), dispatcher=_Dispatcher())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="survey_service.cli", description=__doc__.split("\n")[0])
    ap.add_argument("--bank", required=True, help="QuestionBank JSON 파일")
    ap.add_argument("--db", required=True, help="세션 SQLite 파일")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("turn", help="한 턴 처리")
    t.add_argument("--session", required=True)
    t.add_argument("--turn-id", default=None, help="없으면 새로 만든다. 같은 값을 다시 주면 같은 결과")
    g = t.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", action="store_true")
    g.add_argument("--utterance")
    g.add_argument("--end", action="store_true", help="SESSION_END(사용자 이탈)")
    g.add_argument("--resume", action="store_true", help="개입 결정: 재개")
    g.add_argument("--staff-end", action="store_true", help="개입 결정: 종료")
    t.add_argument("--extraction", default='{"action": "NEXT", "next_item": "END", "values": []}', help="LoRA-A가 낼 ActionExtraction(JSON)")
    a = ap.parse_args(argv)
    bank = json.load(open(a.bank, encoding="utf-8"))
    orch = build(bank, a.db, json.loads(a.extraction))
    tid = a.turn_id or uuid.uuid4().hex
    kw = {}
    if a.resume or a.staff_end:
        kw["intervention_decision"] = {"session_id": a.session, "decision": "RESUME" if a.resume else "END"}
    elif a.start:
        kw["frontend_message"] = {"session_id": a.session, "kind": "SESSION_START"}
    elif a.end:
        kw["frontend_message"] = {"session_id": a.session, "kind": "SESSION_END"}
    else:
        kw["frontend_message"] = {"session_id": a.session, "kind": "UTTERANCE", "utterance": a.utterance}
    out = orch.handle_turn(turn_id=tid, **kw)
    print(json.dumps({"turn_id": tid, "replayed": out.replayed, "frontend_input": out.frontend_input,
                      "orchestrator_output": out.orchestrator_output}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
