"""R7: 공개 API의 응답은 내보내기 전에 계약으로 검증된다(Route.response_schema, openit_link.App이 어기면 503). 계약에 응답 타입이 있는 경로는 모두 걸려 있어야 한다."""
import pytest
from boundary_contracts import validator

from survey_service.components.link_public_api import _SPEC

from .test_staff_alerts import make_app
from .http_server_support import stop_server

EXPECTED = {"state": "SessionState", "staff_retry": "StaffCallRetryResult", "staff_report": "StaffAlert", "alerts": "StaffAlertPage", "push_config": "PushConfig"}


def test_every_response_schema_in_the_table_is_a_contract_schema():
    for row in _SPEC:
        if row[7] is not None:
            assert validator(row[7]) is not None, row


def test_the_contract_response_types_are_attached_to_the_routes(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        by = {r.pattern: r.response_schema for r in app.api.routes()}
        assert by["/v1/staff/push-config"] == "PushConfig" and by["/v1/staff/alerts"] == "StaffAlertPage"
        assert by["/v1/sessions/{sid}/state"] == "SessionState"
        assert by["/v1/sessions/{sid}/staff-call/retry"] == "StaffCallRetryResult" and by["/v1/sessions/{sid}/staff-call/report"] == "StaffAlert"
        assert {n: r for n, *_rest, r in [(row[0], *row[1:7], row[7]) for row in _SPEC if row[7]]} == EXPECTED
    finally:
        app.draining.set(); stop_server(httpd); app.stop()


def test_a_push_config_that_violates_the_contract_is_not_sent(tmp_path, monkeypatch):
    from .test_staff_alerts import http_call
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        app.alerts.public_key = lambda: 123          # PushConfig.vapid_public_key는 문자열 또는 null이다(래퍼는 속성 설정을 대상에 넘긴다)
        code, body = http_call(port, "GET", "/v1/staff/push-config")
        assert code == 503 and body["code"] == "UNAVAILABLE" and "PushConfig" in body["message"]
    finally:
        app.draining.set(); stop_server(httpd); app.stop()
