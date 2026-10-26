"""참조 서버(tests/ref_servers.py)에 FakeReleaseReader로 만든 일관된 릴리스를 올린다. 시험과 스모크가 같이 쓴다."""
import json

import model_runtime
import turn_engine

from .fakes import FakeReleaseReader
from .sample_bank import sample_bank

TOK = {"r-tok": "reader", "w-tok": "survey-service"}


def publish(server, tmp_path, rid, version=1, pointer=True, tamper_bank=False, **kw):
    """FakeReleaseReader로 일관된 릴리스(매니페스트·판정·어댑터)를 만들어 참조 서버에 올린다."""
    fr = FakeReleaseReader(tmp_path / "src")
    fr.add_release(rid, sample_bank(version), turn_engine_version=turn_engine.ENGINE_VERSION,
                   model_runtime_version=model_runtime.RUNTIME_VERSION, **kw)
    m = fr.manifests[rid]
    adapters = {r: {f.name: f.read_bytes() for f in sorted((fr.root / "adapters" / rid / r).iterdir())} for r in ("LORA_A", "LORA_B")}
    bank = fr.banks[rid]
    if tamper_bank:
        bank = json.loads(json.dumps(bank)); bank["session_settings"]["confirm_limit"] += 1
    server.releases[rid] = {"manifest": m, "bank": bank, "adapters": adapters, "pass": fr.decisions[m["gate_decision_id"]]["result"] == "PASS"}
    server.release_order.append(rid)
    server.decisions.append(fr.decisions[m["gate_decision_id"]])
    if pointer:
        v = (server.pointer["pointer_version"] + 1) if server.pointer else 1
        server.pointer = {"pipeline_schema_version": m["pipeline_schema_version"], "release_id": rid, "pointer_version": v,
                          "activated_at": "2026-10-15T00:00:00Z", "cause": "ACTIVATE"}



