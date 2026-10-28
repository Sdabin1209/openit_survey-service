"""단계 B (4): 세션 보기 `StaffSessionView.alerts[].kind`는 목록 `StaffAlert`와 같은 이름(RISK_STOP, CALL_STAFF, USER_CALLED_STAFF)을 쓴다.

기대값의 출처: ../boundary-contracts/PUBLIC_API.md 48행(2.2.0): "목록 StaffAlert와 세션 보기 StaffSessionView.alerts[]는 같은 이름을 쓴다".

**막힘(CR-019)**: 이 영역의 문진 스키마 `schema/schemas/StaffSessionView.json`의 `AlertKind`가 RISK, CALL_STAFF, USER_CALLED_STAFF만 허용한다. 코드만 고쳐
RISK_STOP을 내면 스키마 위반이다(둘째 시험). 지시에 따라 스키마는 고치지 않았다. 아래 시험은 strict xfail로 현재 불일치를 기록한다:
스키마가 고쳐지고 코드가 맞춰지면 XPASS(strict)로 실패해 이 표식을 지우게 한다.
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


@pytest.mark.xfail(strict=True, reason="CR-019: StaffSessionView 스키마의 AlertKind가 RISK를 쓴다. 스키마 변경 요청 대기")
def test_view_alert_kind_equals_the_list_alert_kind(vr):
    _risk(vr)
    _, lst, _ = vr.call("GET", "/v1/staff/alerts")
    _, view, _ = vr.call("GET", "/v1/staff/sessions/K1")
    assert [a["kind"] for a in lst["items"]] == ["RISK_STOP"]
    assert [a["kind"] for a in view["alerts"]] == ["RISK_STOP"]


def test_blocker_view_schema_rejects_the_contract_name_today(vr):
    """막힘의 증명: 계약 이름 RISK_STOP을 담은 세션 보기는 현재 스키마가 거부한다."""
    _risk(vr)
    _, view, _ = vr.call("GET", "/v1/staff/sessions/K1")
    view["alerts"][0]["kind"] = "RISK_STOP"
    assert list(VIEW.iter_errors(view)), "스키마가 RISK_STOP을 허용하게 바뀌었다: CR-019가 처리됐다. 위 xfail을 지우고 api.py의 역변환을 없애라"
