"""L28: pause_reason(RISK|OTHER)가 StaffSessionView(세션 보기·alerts[])·SessionState·알림 목록·TurnFailureSignal·턴 지표(outcome=paused)에 실린다."""
import pytest

from survey_service.components.metrics_export import Metrics, turn_outcome

from .test_public_api_v132 import valid  # noqa: F401  (SessionState 검증기)
from .test_results_signals_retention import AUTH, STAFF, VIEW_V, env  # noqa: F401


@pytest.mark.parametrize("severity,expected", [("RISK", "RISK"), ("CALL_STAFF", "OTHER")])
def test_pause_reason_is_carried_on_every_surface(env, severity, expected):
    env.start("K1")
    sig = {"signal_id": "sg-1", "source": "clf-a", "code": "C7", "severity": severity}
    assert env.signal("K1", sig)[0] == 201
    st, state, _ = env.call("GET", "/v1/sessions/K1/state", auth=AUTH)
    assert st == 200 and state["state"] == "PAUSED_RISK" and state["pause_reason"] == expected
    st, view, _ = env.call("GET", "/v1/staff/sessions/K1")
    assert st == 200 and not list(VIEW_V.iter_errors(view)) and view["pause_reason"] == expected
    assert [a["pause_reason"] for a in view["alerts"]] == [expected]
    st, lst, _ = env.call("GET", "/v1/staff/alerts")
    assert [a["pause_reason"] for a in lst["items"]] == [expected]


def test_pause_reason_is_absent_when_not_paused(env):
    env.start("K1")
    assert "pause_reason" not in env.call("GET", "/v1/sessions/K1/state", auth=AUTH)[1]
    assert "pause_reason" not in env.call("GET", "/v1/staff/sessions/K1")[1]


def test_turn_metric_label_is_paused_whenever_a_pause_reason_is_on_the_signal():
    for alert in ("DELIVERED", "DELIVERY_FAILED", "NONE"):
        assert turn_outcome({"turn_failure_signal": {"write_committed": "COMMITTED", "staff_alert": alert, "pause_reason": "RISK"}}) == "paused"
    assert turn_outcome({"turn_failure_signal": {"write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}}) == "failed"
    m = Metrics()
    m.observe_turn(turn_outcome({"turn_failure_signal": {"write_committed": "COMMITTED", "staff_alert": "DELIVERY_FAILED", "pause_reason": "OTHER"}}), 0.2)
    assert 'openit_turn_duration_seconds_count{outcome="paused"} 1.0' in m.render()[0].decode()


def test_results_api_carries_pause_reason_only_while_paused(env):
    from .test_results_signals_retention import RESULT, RESULT_V
    env.start("K1")
    st, r, _ = env.call("GET", "/v1/results/K1", auth=RESULT)
    assert st == 200 and "pause_reason" not in r and not list(RESULT_V.iter_errors(r))
    env.signal("K1", {"signal_id": "sg-1", "source": "clf-a", "code": "C7"})
    st, r, _ = env.call("GET", "/v1/results/K1", auth=RESULT)
    assert r["status"] == "PAUSED_RISK" and r["pause_reason"] == "RISK" and not list(RESULT_V.iter_errors(r))
    items = env.call("GET", "/v1/results", auth=RESULT)[1]["items"]
    assert items[0]["pause_reason"] == "RISK"
