"""문진 스키마의 사본 규칙: 어느 스키마 파일의 `$defs/<이름>`이든, `schemas/<이름>.json`(정본)이 있으면 정본의 `$defs/<이름>`과 같아야 한다(안쪽 $ref까지 풀어서 견준다).
사본이 낡아 세션 시작 Trace 봉투 검증이 503이던 결함(4.0.0의 FrontendMessage 변경이 TurnTrace·OrchestratorInput 사본에 안 갔다, 4.1.1)의 재발 방지."""
from boundary_contracts import schema_validator
import copy
import glob
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _inline(node, defs, seen=()):
    if isinstance(node, dict):
        r = node.get("$ref")
        if isinstance(r, str) and r.startswith("#/$defs/"):
            n = r.rsplit("/", 1)[1]
            if n in seen:
                return {"$cycle": n}
            return {**{k: _inline(v, defs, seen) for k, v in node.items() if k != "$ref"}, "$def": n, "=": _inline(defs[n], defs, seen + (n,))}
        return {k: _inline(v, defs, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v, defs, seen) for v in node]
    return node


def stale_copies(schemas: dict) -> list:
    out = []
    for name, doc in schemas.items():
        canon = doc.get("$defs", {}).get(name)
        if canon is None:
            continue
        ci = _inline(canon, doc["$defs"])
        for other, od in schemas.items():
            d = od.get("$defs", {}).get(name)
            if other != name and d is not None and _inline(d, od["$defs"]) != ci:
                out.append(f"{other}.json의 $defs/{name}이 {name}.json과 다르다")
    return out


def _load():
    return {os.path.basename(f)[:-5]: json.load(open(f, encoding="utf-8")) for f in glob.glob(str(ROOT / "schema" / "schemas" / "*.json"))}


def test_every_schema_def_copy_equals_its_canonical_schema():
    assert stale_copies(_load()) == []


def test_a_deliberately_stale_copy_is_detected():
    s = _load()
    bad = copy.deepcopy(s)
    start = next(b for b in bad["OrchestratorInput"]["$defs"]["FrontendMessage"]["oneOf"] if b["properties"]["kind"].get("const") == "SESSION_START")
    del start["properties"]["question_set_id"]
    found = stale_copies(bad)
    assert "OrchestratorInput.json의 $defs/FrontendMessage이 FrontendMessage.json과 다르다" in found      # 이 복사를 품은 다른 사본(TurnTrace의 OrchestratorInput)도 함께 걸린다


def test_session_start_with_question_set_id_passes_the_trace_envelope_source_check():
    """E2E 5단계의 503: SESSION_START + question_set_id를 담은 Trace가 TraceEnvelopeSource 검증을 통과한다."""
    import survey_schema
    from boundary_contracts import schema_validator as Draft202012Validator
    msg = {"kind": "SESSION_START", "question_set_id": "health-screening", "subject_ref": "PT-ref#001"}
    assert list(Draft202012Validator(survey_schema.load_schema("FrontendMessage")).iter_errors(msg)) == []
    ex = json.loads((ROOT / "schema/schemas/examples/TurnTrace.valid.json").read_text(encoding="utf-8"))
    ex["orchestrator_input"]["frontend_input"] = msg
    ex["orchestrator_input"]["intervention_decision"] = None
    from survey_service import types
    types.validate("TraceEnvelopeSource", {"release_id": "rel-1", "trace": ex, "now": 1.0, "question_set_id": "health-screening"})


def test_session_start_with_question_set_id_over_the_real_app_with_trace_envelope(tmp_path, monkeypatch):
    """운영 구성(세션 DB와 같은 DB의 Trace outbox, 원자적 봉투)에서 POST /v1/sessions + question_set_id가 200이다(E2E 5단계의 503 재현 경로)."""
    from survey_service.settings import Settings
    from survey_service.wiring.app import App
    from .fakes import FakeReleaseReader, FakeTraceIngest
    from .sample_bank import sample_bank
    monkeypatch.delenv("SURVEY_DATABASE_URL", raising=False)
    monkeypatch.setenv("SURVEY_API_TOKEN", "tok-0123456789abcdef"); monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "stok-0123456789abcdef")
    rd = FakeReleaseReader(tmp_path / "gate")
    rd.add_release("rel-1", [sample_bank(question_set_id="health-screening", title="건강검진"), sample_bank(question_set_id="health-screening-b", title="B", order=["Q3", "Q4"])])
    rd.set_pointer("rel-1")
    s = Settings.from_env({"SURVEY_RELEASE_READER": "http", "SURVEY_RELEASE_READER_URL": "http://127.0.0.1:1", "SURVEY_TRACE_INGEST": "http", "SURVEY_TRACE_INGEST_URL": "http://127.0.0.1:1", "SURVEY_MODEL_BACKEND": "fake", "SURVEY_WORK_DIR": str(tmp_path / "w"),
                           "SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_API_TOKEN": "tok-0123456789abcdef", "SURVEY_STAFF_API_TOKEN": "stok-0123456789abcdef"})
    app = App(s, reader=rd, ingest=FakeTraceIngest())
    try:
        app.loader.poll_once()
        assert app.orchestrator.atomic_trace()
        for body, want in (({"kind": "SESSION_START", "question_set_id": "health-screening", "subject_ref": "PT-ref#001"}, 200), ({"kind": "SESSION_START"}, 400)):
            code, out, _ = app.api.handle("POST", "/v1/sessions", "Bearer tok-0123456789abcdef", json.dumps(body).encode(), device="test-kiosk")
            assert code == want, (code, out)
    finally:
        app.stop()
