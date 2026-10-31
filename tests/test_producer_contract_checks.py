"""R7(생산자 검증): 직원 웹 푸시 본문은 보내기 전에 계약 StaffPushPayload로, /readyz 본문은 내보내기 전에 계약 SurveyReadyz로 검증한다. 위반 출력은 나가지 않는다."""
import json

from survey_service.components.alert_service import AlertHub, AlertService

from .test_readyz_db_outage import _app
from .timing import fake_clock


class Store:
    def subscriptions(self):
        return [{"endpoint": "https://push.example.com/a", "keys": {"p256dh": "a", "auth": "b"}}]

    def remove_subscription(self, endpoint):
        return True


class Push:
    def __init__(self):
        self.sent = []

    def send(self, sub, payload):
        self.sent.append(json.loads(payload))
        return "ACCEPTED"


def _service():
    push = Push()
    return AlertService(Store(), AlertHub(), push, push_timeout=1.0, monotonic=fake_clock()), push


def test_a_valid_push_payload_is_sent():
    svc, push = _service()
    assert svc._push({"alert": {"alert_id": "al-1", "session_id": "S1", "kind": "RISK_STOP"}}) == 1
    assert push.sent == [{"alert_id": "al-1", "session_id": "S1", "kind": "RISK_STOP"}]


def test_a_push_payload_that_violates_the_contract_is_not_sent():
    svc, push = _service()
    assert svc._push({"alert": {"alert_id": "", "session_id": "S1", "kind": "RISK_STOP"}}) == 0
    assert push.sent == [] and svc.stats["push_failed"] >= 1


def test_a_readyz_body_that_violates_the_contract_is_not_ready_and_carries_no_values(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    try:
        assert app.readiness()[0] is True
        monkeypatch.setattr(app, "_reask_limit", lambda: {"active": "비밀값-xyz", "max_consecutive": 3, "effective": None})      # reask_limit.active는 불리언이다
        ok, body = app.readiness()
        assert ok is False and body["problems"] == ["readyz_contract_violation"]
        assert "reask_limit/active" in body["violation_paths"] and "비밀값-xyz" not in json.dumps(body, ensure_ascii=False)
    finally:
        app.stop()
