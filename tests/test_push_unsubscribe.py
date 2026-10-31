"""단계 B (2): 푸시 해지는 `POST /v1/staff/push-subscriptions/unsubscribe` 본문 {"endpoint"} -> 204.

기대값의 출처: ../boundary-contracts/PUBLIC_API.md 45행(2.2.0): 204, 푸시 엔드포인트 URL은 그 자체가 푸시 권한을 주는 값이라 URL에 싣지 않는다,
기존 `DELETE ...?endpoint=`는 2.x에서만 허용하는 폐기 예정 방식. 지시: 없는 endpoint도 멱등으로 204, 폐기 예정 방식은 유지하고 로그에 경고,
푸시 엔드포인트 URL은 로그에 남기지 않는다. HTTP 204는 본문이 없어야 한다(RFC 9110 15.3.5).
"""
import logging
import urllib.request

import pytest

from .timing import HANG_GUARD_SECONDS
from .test_staff_alerts import AlertRig, http_call, live  # noqa: F401  (live: 실제 HTTP 서버 fixture)

EP = "https://push.example/secret-endpoint-token-123"
SUB = {"endpoint": EP, "keys": {"p256dh": "a", "auth": "b"}}
UNSUB = "/v1/staff/push-subscriptions/unsubscribe"


@pytest.fixture
def ar(tmp_path):
    r = AlertRig(tmp_path)
    yield r
    r.store.close()


def test_unsubscribe_by_post_body_returns_204_and_removes(ar):
    assert ar.call("POST", "/v1/staff/push-subscriptions", SUB)[0] == 201
    code, body, _ = ar.call("POST", UNSUB, {"endpoint": EP})
    assert code == 204 and body is None
    assert ar.store.count_subscriptions() == 0


def test_unsubscribe_unknown_endpoint_is_idempotent_204(ar):
    assert ar.call("POST", UNSUB, {"endpoint": EP})[0] == 204
    assert ar.call("POST", UNSUB, {"endpoint": EP})[0] == 204


@pytest.mark.parametrize("body", [{}, {"endpoint": 5}, {"endpoint": ""}, {"endpoint": EP, "x": 1}])
def test_unsubscribe_rejects_bad_body(ar, body):
    assert ar.call("POST", UNSUB, body)[0] == 400


def test_unsubscribe_needs_staff_role(ar):
    from .test_staff_alerts import AUTH
    assert ar.call("POST", UNSUB, {"endpoint": EP}, auth=AUTH)[0] == 403
    assert ar.call("POST", UNSUB, {"endpoint": EP}, auth="")[0] == 401


def test_deprecated_delete_still_works_and_logs_a_warning_without_the_url(ar, caplog):
    ar.call("POST", "/v1/staff/push-subscriptions", SUB)
    caplog.set_level(logging.DEBUG)
    code, body, _ = ar.call("DELETE", "/v1/staff/push-subscriptions?endpoint=" + EP)
    assert code == 200 and body["deleted"] is True
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "폐기 예정" in r.getMessage()]
    assert len(warns) == 1


def test_endpoint_url_never_appears_in_logs(ar, caplog):
    caplog.set_level(logging.DEBUG)
    ar.call("POST", "/v1/staff/push-subscriptions", SUB)
    ar.call("POST", UNSUB, {"endpoint": EP})
    ar.call("POST", "/v1/staff/push-subscriptions", SUB)
    ar.call("DELETE", "/v1/staff/push-subscriptions?endpoint=" + EP)
    ar.call("POST", UNSUB, {"endpoint": EP + "-missing"})
    assert "secret-endpoint-token-123" not in caplog.text


def test_204_over_http_has_no_body(live):  # noqa: F811
    app, port, _ = live
    http_call(port, "POST", "/v1/staff/push-subscriptions", SUB)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{UNSUB}", method="POST", data=b'{"endpoint": "%s"}' % EP.encode(),
                                 headers={"Authorization": "Bearer test-staff-token", "X-Openit-User": "staff-1"})
    with urllib.request.urlopen(req, timeout=HANG_GUARD_SECONDS) as r:
        assert r.status == 204 and r.read() == b"" and r.headers.get("Content-Length") in (None, "0")
    assert app.alert_store.count_subscriptions() == 0
