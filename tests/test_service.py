"""서비스 조립, 설정, /healthz /readyz."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from survey_service.settings import ConfigError, Settings, load_factory
from survey_service.service import App, serve

from .http_server_support import server_port, start_server, stop_server
from .fakes import FakeReleaseReader, FakeTraceIngest, RecordingConsole, RecordingDispatcher
from .sample_bank import sample_bank


def settings(tmp_path, **kw):
    base = dict(session_db=str(tmp_path / "s.db"), outbox_db=str(tmp_path / "o.db"), work_dir=str(tmp_path / "w"),
                release_reader="x:y", trace_ingest="x:y", model_backend="fake", release_poll_seconds=0.05,
                export_interval_seconds=0.05)
    base.update(kw)
    return Settings(**base)


def get(port, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=b"{}" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def running(tmp_path):
    reader = FakeReleaseReader(tmp_path / "gate")
    ingest = FakeTraceIngest()
    app = App(settings(tmp_path), reader=reader, ingest=ingest, staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
    httpd = serve(app, 0)
    port = server_port(httpd)
    start_server(httpd)
    yield app, reader, ingest, port
    stop_server(httpd)
    app.stop()


def test_healthz_is_always_ok_and_readyz_waits_for_release(running):
    app, reader, ingest, port = running
    assert get(port, "/healthz") == (200, {"status": "ok"})
    code, body = get(port, "/readyz")
    assert code == 503 and body["ready"] is False and "no_active_release" in body["problems"]
    reader.add_release("rel-1", sample_bank())
    reader.set_pointer("rel-1")
    app.start()
    deadline = time.time() + 5
    while time.time() < deadline and get(port, "/readyz")[0] != 200:
        time.sleep(0.05)
    code, body = get(port, "/readyz")
    assert code == 200 and body["ready"] and body["active_release"] == "rel-1" and body["contract_version"] == 1
    assert body["model_backend"] == "fake" and body["model_backend_production_safe"] is False
    assert body["trace_outbox"]["pending"] == 0


def test_readyz_not_ready_when_pointer_points_to_unsupported_version(running):
    app, reader, ingest, port = running
    reader.add_release("rel-9", sample_bank(), runtime_contract_version=2)
    reader.set_pointer("rel-9")
    app.loader.poll_once()
    code, body = get(port, "/readyz")
    assert code == 503 and body["release_loader"]["rejections"][-1]["code"] == "UNSUPPORTED_VERSION"


def test_no_public_api_only_health_endpoints(running):
    app, reader, ingest, port = running
    for path in ("/v1/sessions", "/v1/turns", "/", "/metrics"):
        assert get(port, path)[0] == 404
    assert get(port, "/v1/turns", "POST")[0] == 404 and get(port, "/healthz", "POST")[0] == 404


def test_turn_through_app_reaches_ingest(running):
    app, reader, ingest, port = running
    reader.add_release("rel-1", sample_bank())
    reader.set_pointer("rel-1")
    app.start()
    out = app.orchestrator.handle_turn(turn_id="a1", frontend_message={"session_id": "S1", "kind": "SESSION_START"})
    assert out.release_id == "rel-1"
    deadline = time.time() + 5
    while time.time() < deadline and ("S1", "a1") not in ingest.received:
        time.sleep(0.05)
    env = ingest.received[("S1", "a1")]
    assert env["release_id"] == "rel-1" and env["runtime_contract_version"] == 1


def test_settings_from_env():
    env = {"SURVEY_RELEASE_READER": "a:b", "SURVEY_TRACE_INGEST": "c:d", "SURVEY_MODEL_BACKEND": "fake", "SURVEY_PORT": "9000",
           "SURVEY_KEEP_RELEASES": "4"}
    s = Settings.from_env(env)
    assert s.port == 9000 and s.keep_releases == 4 and s.session_db == "/data/sessions.db" and s.release_poll_seconds == 5.0


@pytest.mark.parametrize("env", [
    {}, {"SURVEY_RELEASE_READER": "a:b", "SURVEY_TRACE_INGEST": "c:d"},
    {"SURVEY_RELEASE_READER": "a:b", "SURVEY_TRACE_INGEST": "c:d", "SURVEY_MODEL_BACKEND": "fake", "SURVEY_PORT": "x"},
    {"SURVEY_RELEASE_READER": "a:b", "SURVEY_TRACE_INGEST": "c:d", "SURVEY_MODEL_BACKEND": "fake", "SURVEY_RELEASE_POLL_SECONDS": "0"},
])
def test_settings_reject_missing_or_bad(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_load_factory():
    assert load_factory("tests.smoke_fakes:console")().__class__.__name__ == "RecordingConsole"
    for bad in ("nocolon", "no.such.module:x", "tests.smoke_fakes:nothing"):
        with pytest.raises(ConfigError):
            load_factory(bad)


def test_unknown_model_backend_refuses_to_start(tmp_path):
    from survey_service.inference import InferenceLoadError
    with pytest.raises(InferenceLoadError):
        App(settings(tmp_path, model_backend="vllm"), reader=object(), ingest=object())


def test_cli_harness_keeps_session_between_calls(tmp_path, capsys):
    from survey_service import cli
    bank = tmp_path / "bank.json"
    bank.write_text(json.dumps(sample_bank(), ensure_ascii=False), encoding="utf-8")
    base = ["--bank", str(bank), "--db", str(tmp_path / "c.db")]
    assert cli.main(base + ["turn", "--session", "S1", "--start", "--turn-id", "c1"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["orchestrator_output"]["session_info_created"]["session_id"] == "S1"
    ex = json.dumps({"action": "NEXT", "next_item": "Q1.D02", "values": [
        {"key": "Q1.D01.diagnosed", "value": False, "precision": "exact", "status": "ANSWERED", "save": True},
        {"key": "Q1.D01.on_medication", "value": False, "precision": "exact", "status": "ANSWERED", "save": True}]})
    assert cli.main(base + ["turn", "--session", "S1", "--utterance", "안 받았고 안 먹어요", "--extraction", ex, "--turn-id", "c2"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["orchestrator_output"]["log_entry"]["extract_output"]["next_item"] == "Q1.D02"
    assert second["frontend_input"]["response_text"] == "(가짜 응답)"
    assert cli.main(base + ["turn", "--session", "S1", "--start", "--turn-id", "c1"]) == 0
    assert json.loads(capsys.readouterr().out)["replayed"] is True


def test_readyz_says_the_staff_alert_source_is_missing_instead_of_silently_disabling(running):
    """D74: v2의 A 출력에는 위험 신호가 없어 직원 알림의 입력원이 없다(CR-012). 알림 경로는 살아 있지만 알림이 만들어지지 않는다는 사실을 /readyz가 낸다."""
    app, reader, ingest, port = running
    body = get(port, "/readyz")[1]
    src = body["staff_alert_source"]
    assert src["active"] is True and src["provisional"] is True and src["clinically_reviewed"] is False      # 임시 기본 목록이 기본으로 켜진다(D88 U2)
    assert "staff_alerts" in body                                              # 저장·전달 경로의 통계는 그대로 있다
    assert body["response_tables"]["status"] == "DATASET_RULES_UNCONFIRMED" and set(body["response_tables"]["counters"]) == {"ask_fallback", "phrase_fallback", "ask_generic", "phrase_generic"} and body["question_flow"] == {"flow_id": "dataset_v2", "items": 31}


def test_risk_source_factory_setting_activates_the_alert_path(tmp_path):
    app = App(settings(tmp_path, risk_source="tests.smoke_fakes:risk_source"), reader=FakeReleaseReader(tmp_path / "gate"),
              ingest=FakeTraceIngest(), staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
    try:
        assert app.readiness()[1]["staff_alert_source"] == {"active": True}
    finally:
        app.stop()
