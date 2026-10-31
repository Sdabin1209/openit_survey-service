"""요청마다 계약 검증기를 새로 만들지 않는다(E2E 5pn: validator()는 호출마다 스키마 파일을 다시 읽는다). 만든 횟수를 가짜로 센다(실제 시간을 재지 않는다)."""
from survey_service.components import alert_service
from survey_service.wiring import app as app_mod

from .test_readyz_db_outage import _app


def test_readyz_validator_is_built_once_at_startup_not_per_request(tmp_path, monkeypatch):
    calls = []
    real = app_mod.validator
    monkeypatch.setattr(app_mod, "validator", lambda name: (calls.append(name), real(name))[1])
    app = _app(tmp_path, monkeypatch)
    try:
        built = calls.count("SurveyReadyz")
        for _ in range(3):
            app.readiness()
        assert built == 1 and calls.count("SurveyReadyz") == 1
    finally:
        app.stop()


def test_push_payload_validator_is_built_once_per_service_not_per_push(monkeypatch):
    calls = []
    real = alert_service.validator
    monkeypatch.setattr(alert_service, "validator", lambda name: (calls.append(name), real(name))[1])
    svc = alert_service.AlertService(store=None, monotonic=lambda: 0.0)
    assert calls == ["StaffPushPayload"]
    assert svc._push_validator is not None
