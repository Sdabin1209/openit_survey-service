"""G01: 직원 알림 입력원은 포트(RiskSource)로 분리되어 있고 SURVEY_RISK_SOURCE로 구현을 고른다. 기본은 비활성. 입력원은 정해지지 않았다."""
from survey_service.alerts import AlertHub, AlertService, AlertStaffConsole, AlertStore
from survey_service.service import App
from survey_service.settings import Settings, load_factory

from .e2e_support import Rig, ext


class FakeInputSource:
    """포트 계약 시험용 가짜 입력원(운영 입력원이 아니다)."""
    def __init__(self) -> None:
        self.calls = []

    def check(self, *, extract_input, extract_output, utterance):
        self.calls.append(utterance)
        return {"kind": "CALL_STAFF", "reasons": []} if "불러" in utterance else None


def fake_source():
    return FakeInputSource()


def _app(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("SURVEY_API_TOKEN", "a-tok")
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "s-tok")
    s = Settings(session_db=str(tmp_path / "s.db"), outbox_db=str(tmp_path / "o.db"), work_dir=str(tmp_path / "w"), model_backend="fake", **kw)
    risk = load_factory(s.risk_source)() if s.risk_source else None
    return App(s, reader=object(), ingest=object(), risk_source=risk)


def test_default_is_inactive_and_configured_source_is_reported_active(tmp_path, monkeypatch):
    off = _app(tmp_path / "a", monkeypatch)
    assert off.orchestrator.risk_source is None and off.readiness()[1]["staff_alert_source"]["active"] is False
    on = _app(tmp_path / "b", monkeypatch, risk_source="tests.test_alert_source_port:fake_source")
    assert isinstance(on.orchestrator.risk_source, FakeInputSource) and on.readiness()[1]["staff_alert_source"]["active"] is True


def test_alert_flows_from_fake_source_to_alert_store(tmp_path):
    src = FakeInputSource()
    rig = Rig(risk_source=src)
    store = AlertStore(str(tmp_path / "alerts.db"))
    rig.orch.staff_console = AlertStaffConsole(AlertService(store, AlertHub()))       # 기본 조립과 같은 직원 접점
    rig.start()
    rig.say("괜찮아요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
    assert store.list(None, 10, None)[0] == []
    rig.say("직원 불러 주세요", out=ext("REDIRECT", "Q1.D01", reason="OFF_TOPIC"))
    alerts = store.list(None, 10, None)[0]
    assert [a["kind"] for a in alerts] == ["CALL_STAFF"] and alerts[0]["session_id"] == "S1" and src.calls == ["괜찮아요", "직원 불러 주세요"]
