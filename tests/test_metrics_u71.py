"""U71: 운영 소요 시간 지표(계약 METRICS.md). 한 턴 뒤 내부망 GET /metrics에 두 이름이 나오고, 게이트웨이로는 나가지 않는다 (ADR-0055)."""
import json
import urllib.request
from pathlib import Path

from survey_service.components.link_inference import Stats
from survey_service.components.metrics_export import Metrics, turn_outcome

from .http_server_support import server_port, start_server, stop_server
from .sample_bank import sample_bank
from .test_service import HANG_GUARD_SECONDS, get, running, settings  # noqa: F401


def _metrics_text(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=HANG_GUARD_SECONDS) as r:
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/plain")
        return r.read().decode()


def test_metrics_endpoint_has_both_names_after_a_turn(running):
    app, reader, ingest, port = running
    reader.add_release("rel-1", sample_bank())
    reader.set_pointer("rel-1")
    app.start()
    app.orchestrator.handle_turn(turn_id="a1", frontend_message={"session_id": "S1", "kind": "SESSION_START"})
    text = _metrics_text(port)
    assert "openit_turn_duration_seconds_count{outcome=\"ok\"} 1.0" in text           # 턴 하나가 ok로 한 번 센다
    assert "openit_model_call_duration_seconds_bucket" in text                       # 모델 호출 히스토그램 이름(라벨 op·outcome)
    assert 'op="extract"' in text and 'outcome="timeout"' in text


def test_replayed_turn_is_not_counted_again(running):
    app, reader, ingest, port = running
    reader.add_release("rel-1", sample_bank())
    reader.set_pointer("rel-1")
    app.start()
    msg = {"session_id": "S1", "kind": "SESSION_START"}
    app.orchestrator.handle_turn(turn_id="a1", frontend_message=msg)
    app.orchestrator.handle_turn(turn_id="a1", frontend_message=msg)                  # 같은 turn_id 재전송: 저장된 결과를 재생한다
    assert "openit_turn_duration_seconds_count{outcome=\"ok\"} 1.0" in _metrics_text(port)


def test_model_call_observer_records_op_and_outcome():
    m = Metrics()
    stats = Stats(observer=m.observe_model_call)
    stats.record_call("extract", 1, 0.4, True, False)
    stats.record_call("respond", 1, 26.0, False, True)
    stats.record_call("extract", 1, 0.2, False, False, "invalid")
    stats.record_call("respond", 2, 0.1, False, False)
    text = m.render()[0].decode()
    for line in ('openit_model_call_duration_seconds_count{op="extract",outcome="ok"} 1.0', 'openit_model_call_duration_seconds_count{op="respond",outcome="timeout"} 1.0',
                 'openit_model_call_duration_seconds_count{op="extract",outcome="invalid"} 1.0', 'openit_model_call_duration_seconds_count{op="respond",outcome="unavailable"} 1.0'):
        assert line in text


def test_turn_outcome_names():
    assert turn_outcome({"response_text": "x", "turn_failure_signal": None}) == "ok"
    assert turn_outcome({"turn_failure_signal": {"write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}}) == "failed"
    assert turn_outcome({"turn_failure_signal": {"write_committed": "COMMITTED", "staff_alert": "DELIVERED", "pause_reason": "RISK"}}) == "paused"


def test_metrics_path_is_denied_at_the_gateway_and_labels_carry_no_ids():
    routes = json.loads((Path(__file__).resolve().parent.parent / "deploy-exposure.json").read_text(encoding="utf-8"))["routes"]
    assert any("/metrics" in r["paths"] and r["auth"]["kind"] == "denied" for r in routes)
    m = Metrics()
    m.observe_turn("ok", 0.1)
    assert "session" not in m.render()[0].decode().lower().replace("session-", "")
