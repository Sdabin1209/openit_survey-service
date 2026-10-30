"""실제 HTTP 클라이언트(boundary_contracts.clients)와 참조 서버(boundary-contracts의 conformance.servers)로 로더와 Trace 전송을 통합 시험한다.

참조 서버는 release-gate·ops-feedback이 아니다. 실제 서버를 띄운 E2E는 deploy의 몫이다.
"""
import json
import time

import pytest
from boundary_contracts.clients import HttpReleaseReader, HttpTraceIngest

from survey_service.components.link_boundary_clients import http_release_reader, http_trace_ingest
from survey_service.components.link_inference import InferenceServer
from survey_service.wiring.orchestrator import Orchestrator
from survey_service.components.release_loader import ReleaseLoader
from survey_service.components.release_registry import ReleaseRegistry
from survey_service.wiring.app import App, _spec
from survey_service.settings import ConfigError, Settings
from survey_service.components.session_store import MemorySessionStore
from survey_service.components.trace_outbox import Outbox
from survey_service.components.trace_envelope import OutboxTraceSink, envelope_id
from survey_service.components.link_trace_export import TraceExporter

from .fakes import FakeReleaseReader, RecordingConsole, RecordingDispatcher
from .http_support import TOK, publish
from conformance.servers import ReferenceServer
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED


@pytest.fixture
def srv():
    with ReferenceServer(TOK) as s:
        yield s


@pytest.fixture
def loader_env(srv, tmp_path):
    reg = ReleaseRegistry()
    loader = ReleaseLoader(HttpReleaseReader(srv.url, "r-tok", 5), reg, InferenceServer("fake"), workdir=tmp_path / "work",
                           installed_versions=INSTALLED, retry_base_seconds=0)
    return srv, reg, loader


def test_loader_receives_bundle_over_http_and_uses_its_paths(loader_env, tmp_path):
    srv, reg, loader = loader_env
    publish(srv, tmp_path, "rel-1")
    assert loader.poll_once().action == "LOADED"
    rel = reg.active()
    assert rel.release_id == "rel-1" and rel.bank == sample_bank()
    assert (tmp_path / "work" / "rel-1" / "adapters" / "LORA_A" / "adapter.bin").read_bytes() == b"rel-1-LORA_A"
    assert any(p.endswith("/bundle") for _, p, _ in srv.requests)
    assert loader.poll_once().action == "UNCHANGED"


def test_pointer_change_and_rollback_over_http(loader_env, tmp_path):
    srv, reg, loader = loader_env
    publish(srv, tmp_path, "rel-1", 1)
    loader.poll_once()
    publish(srv, tmp_path, "rel-2", 2)
    assert loader.poll_once().action == "LOADED" and reg.active_id() == "rel-2"
    srv.pointer = dict(srv.pointer, release_id="rel-1", pointer_version=3, cause="ROLLBACK")
    assert loader.poll_once().action == "ACTIVATED" and reg.active_id() == "rel-1"


def test_unavailable_keeps_previous_release_and_retries(loader_env, tmp_path):
    srv, reg, loader = loader_env
    publish(srv, tmp_path, "rel-1", 1)
    loader.poll_once()
    unavailable = (503, "application/json", json.dumps({"pipeline_schema_version": "1.2", "code": "UNAVAILABLE", "message": "x", "retryable": True}).encode())
    srv.overrides["/v1/active-pointer"] = unavailable
    r = loader.poll_once()
    assert r.action == "RETRY_LATER" and r.code == "UNAVAILABLE" and reg.active_id() == "rel-1"
    del srv.overrides["/v1/active-pointer"]
    publish(srv, tmp_path, "rel-2", 2)
    srv.overrides["/v1/releases/rel-2/bundle"] = unavailable
    assert loader.poll_once().action == "RETRY_LATER" and reg.active_id() == "rel-1"      # 같은 포인터를 다시 시도하게 둔다
    del srv.overrides["/v1/releases/rel-2/bundle"]
    assert loader.poll_once().action == "LOADED" and reg.active_id() == "rel-2"


def test_client_integrity_error_rejects_and_keeps_previous(loader_env, tmp_path):
    srv, reg, loader = loader_env
    publish(srv, tmp_path, "rel-1", 1)
    loader.poll_once()
    publish(srv, tmp_path, "rel-2", 2, tamper_bank=True)          # 서버가 준 문항 세트가 매니페스트 해시와 다르다
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "INTEGRITY_ERROR" and reg.active_id() == "rel-1"
    assert loader.poll_once().action == "UNCHANGED"


def test_non_pass_release_is_not_loaded_over_http(loader_env, tmp_path):
    srv, reg, loader = loader_env
    publish(srv, tmp_path, "rel-1", 1, decision_result="REJECT")
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "NO_APPROVAL" and reg.active() is None
    assert not any(p.endswith("/bundle") for _, p, _ in srv.requests)     # 번들은 받지도 않았다


def test_wrong_token_is_a_rejection_not_a_crash(srv, tmp_path):
    publish(srv, tmp_path, "rel-1")
    loader = ReleaseLoader(HttpReleaseReader(srv.url, "nope", 5), ReleaseRegistry(), InferenceServer("fake"), workdir=tmp_path / "w",
                           installed_versions=INSTALLED, retry_base_seconds=0)
    r = loader.poll_once()
    assert r.action == "RETRY_LATER" and r.code == "PERMISSION_DENIED"      # 포인터를 읽지 못했을 뿐이다. 서비스는 죽지 않는다
    assert loader.registry.active() is None


def test_traces_flow_over_http_with_retry_only_on_unavailable(srv, tmp_path):
    publish(srv, tmp_path, "rel-1")
    reg = ReleaseRegistry()
    loader = ReleaseLoader(HttpReleaseReader(srv.url, "r-tok", 5), reg, InferenceServer("fake"), workdir=tmp_path / "w", installed_versions=INSTALLED, retry_base_seconds=0)
    loader.poll_once()
    ob = Outbox(str(tmp_path / "ob.db"))
    now = [time.time()]
    clock = lambda: now[0]
    ex = TraceExporter(ob, HttpTraceIngest(srv.url, "w-tok", 5), base_delay=2, max_delay=8, clock=clock)
    orch = Orchestrator(MemorySessionStore(), reg, staff_console=RecordingConsole(), dispatcher=RecordingDispatcher(),
                        trace_sink=OutboxTraceSink(ob, clock))
    sid = "세션 하나"                                       # Id 형식이 아니어서 봉투에서는 x- 해시로 바뀐다
    orch.handle_turn(turn_id="턴 1", frontend_message={"session_id": sid, "kind": "SESSION_START"})
    orch.handle_turn(turn_id="t2", frontend_message={"session_id": sid, "kind": "SESSION_END"})
    unavailable = (503, "application/json", json.dumps({"pipeline_schema_version": "1.2", "code": "UNAVAILABLE", "message": "x", "retryable": True}).encode())
    srv.overrides["/v1/traces"] = unavailable
    r = ex.flush_once()
    assert r.retried == 1 and ob.counts()["PENDING"] == 2 and not srv.traces       # 턴은 이미 끝났고 Trace만 남아 있다
    now[0] += 3
    del srv.overrides["/v1/traces"]
    r = ex.flush_once()
    assert r.sent == 2 and ob.counts() == {"PENDING": 0, "SENT": 2, "DEAD": 0}
    keys = set(srv.traces)
    assert (envelope_id(sid), envelope_id("턴 1")) in keys and (envelope_id(sid), "t2") in keys
    env = srv.traces[(envelope_id(sid), "t2")]
    assert env["release_id"] == "rel-1" and env["runtime_contract_version"] == 1 and env["trace"]["session_id"] == sid
    # 같은 봉투를 다시 보내면 DUPLICATE이고 전달 완료로 본다
    ob._db.execute("UPDATE outbox SET state='PENDING', next_attempt_at=0")
    assert ex.flush_once().duplicate == 2
    ob.close()


def test_non_retryable_ingest_error_goes_dead_over_http(srv, tmp_path):
    publish(srv, tmp_path, "rel-1")
    reg = ReleaseRegistry()
    ReleaseLoader(HttpReleaseReader(srv.url, "r-tok", 5), reg, InferenceServer("fake"), workdir=tmp_path / "w", installed_versions=INSTALLED, retry_base_seconds=0).poll_once()
    ob = Outbox(str(tmp_path / "ob.db"))
    ex = TraceExporter(ob, HttpTraceIngest(srv.url, "r-tok", 5))        # 읽기 토큰으로 쓰기 -> PERMISSION_DENIED, 재시도하지 않는다
    Orchestrator(MemorySessionStore(), reg, staff_console=RecordingConsole(), trace_sink=OutboxTraceSink(ob)).handle_turn(
        turn_id="t1", frontend_message={"session_id": "S1", "kind": "SESSION_START"})
    r = ex.flush_once()
    assert r.dead == 1 and ob.dead_rows()[0]["error"].startswith("PERMISSION_DENIED")
    ob.close()


def test_app_with_http_factories_end_to_end(srv, tmp_path, monkeypatch):
    publish(srv, tmp_path, "rel-1")
    env = {"SURVEY_RELEASE_READER": "http", "SURVEY_RELEASE_READER_URL": srv.url, "SURVEY_RELEASE_READER_TOKEN_ENV": "MY_GATE_TOKEN",
           "SURVEY_TRACE_INGEST": "http", "SURVEY_TRACE_INGEST_URL": srv.url, "SURVEY_TRACE_INGEST_TOKEN_ENV": "MY_OPS_TOKEN",
           "SURVEY_MODEL_BACKEND": "fake", "SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"),
           "SURVEY_WORK_DIR": str(tmp_path / "w"), "SURVEY_RELEASE_POLL_SECONDS": "0.05", "SURVEY_EXPORT_INTERVAL_SECONDS": "0.05"}
    for k, v in {**env, "MY_GATE_TOKEN": "r-tok", "MY_OPS_TOKEN": "w-tok"}.items():       # 팩토리는 환경 변수에서 읽는다
        monkeypatch.setenv(k, v)
    app = App(Settings.from_env(env), staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
    try:
        app.start()
        assert app.readiness()[0] and app.registry.active_id() == "rel-1"
        app.orchestrator.handle_turn(turn_id="a1", frontend_message={"session_id": "S1", "kind": "SESSION_START"})
        for _ in range(100):
            if ("S1", "a1") in srv.traces:
                break
            time.sleep(0.05)
        assert ("S1", "a1") in srv.traces
    finally:
        app.stop()


def test_factories_read_token_only_from_env_and_fail_clearly(monkeypatch):
    with pytest.raises(ConfigError):
        http_release_reader({"SURVEY_RELEASE_READER_URL": "http://x"})                  # 토큰 환경 변수가 없다
    with pytest.raises(ConfigError):
        http_trace_ingest({"SURVEY_TRACE_INGEST_TOKEN": "t"})                           # URL이 없다
    c = http_trace_ingest({"SURVEY_TRACE_INGEST_URL": "http://x", "SURVEY_TRACE_INGEST_TOKEN_ENV": "OPS", "OPS": "tok"})
    assert isinstance(c, HttpTraceIngest) and c._token == "tok"
    assert _spec("http", "release").endswith("http_release_reader") and _spec("a:b", "trace") == "a:b"
    with pytest.raises(ConfigError):
        Settings.from_env({"SURVEY_RELEASE_READER": "http", "SURVEY_TRACE_INGEST": "http", "SURVEY_MODEL_BACKEND": "fake"})
