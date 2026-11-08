"""계약 5.0.0 '새 환경 전제': outbox 에 4.x 봉투가 남아 있으면 조용히 섞이지 않고 /readyz 가 사유 코드로 준비 안 됨을 낸다."""
import json

from boundary_contracts import PIPELINE_SCHEMA_VERSION

from survey_service.components.trace_outbox import Outbox
from .timing import fake_clock


def _env(major_minor, sid):
    return {"pipeline_schema_version": major_minor, "session_id": sid, "turn_id": "t1", "trace": {}}


def test_foreign_major_rows_are_counted_only_while_pending_or_dead(tmp_path):
    ob = Outbox(str(tmp_path / "o.db"))
    clock = fake_clock()
    major = PIPELINE_SCHEMA_VERSION.split(".")[0]
    ob.enqueue(_env(PIPELINE_SCHEMA_VERSION, "S-ok"), clock())
    assert ob.foreign_pipeline_rows(major) == 0
    ob.enqueue(_env("4.0", "S-old"), clock())
    assert ob.foreign_pipeline_rows(major) == 1
    row = [r for r in ob.due(clock() + 1, 10) if r.session_id == "S-old"][0]
    ob.mark_sent(row.id, clock())
    assert ob.foreign_pipeline_rows(major) == 0      # 이미 보낸 기록은 세지 않는다
    ob.close()


def test_readiness_fails_with_a_reason_code_when_a_4x_envelope_is_pending(tmp_path, monkeypatch):
    from .test_staff_alerts import make_app
    from .test_staff_alerts import http_call
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        app.outbox.enqueue(_env("4.0", "S-old"), 1.0)
        ready, body = app.readiness()
        assert not ready and "stored_pipeline_major_mismatch" in body["problems"]
    finally:
        app.draining.set()
        from .http_server_support import stop_server
        stop_server(httpd); app.stop()
