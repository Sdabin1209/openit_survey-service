"""단계 B (4): 세션 보기 `StaffSessionView.alerts[].kind`는 목록 `StaffAlert`와 같은 이름(RISK_STOP, CALL_STAFF, USER_CALLED_STAFF)을 쓴다.

기대값의 출처: ../boundary-contracts/PUBLIC_API.md 48행(2.2.0): "목록 StaffAlert와 세션 보기 StaffSessionView.alerts[]는 같은 이름을 쓴다".
이 영역의 문진 스키마 `schema/schemas/StaffSessionView.json`의 AlertKind도 같은 세 이름이어야 한다(헤드 결정: 이 영역이 자기 schema/를 고친다, CR-019 해결).
"""
import pytest

from .test_staff_view_and_rules import TEST_PHRASE, VIEW, ViewRig, RULES, converse


@pytest.fixture
def vr(tmp_path):
    r = ViewRig(tmp_path, rules=RULES)
    yield r
    r.store.close()


def _risk(vr):
    converse(vr)
    vr.post("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": f"저기 {TEST_PHRASE} 입니다", "turn_id": "t-2"})


def test_view_alert_kind_equals_the_list_alert_kind(vr):
    _risk(vr)
    _, lst, _ = vr.call("GET", "/v1/staff/alerts")
    _, view, _ = vr.call("GET", "/v1/staff/sessions/K1")
    assert [a["kind"] for a in lst["items"]] == ["RISK_STOP"]
    assert [a["kind"] for a in view["alerts"]] == ["RISK_STOP"]


def test_view_with_contract_names_validates_against_the_schema_and_old_name_does_not(vr):
    _risk(vr)
    _, view, _ = vr.call("GET", "/v1/staff/sessions/K1")
    assert not list(VIEW.iter_errors(view))
    for kind in ("RISK_STOP", "CALL_STAFF", "USER_CALLED_STAFF"):
        v = {**view, "alerts": [{**view["alerts"][0], "kind": kind}]}
        assert not list(VIEW.iter_errors(v)), kind
    assert list(VIEW.iter_errors({**view, "alerts": [{**view["alerts"][0], "kind": "RISK"}]}))      # 옛 이름은 거부
