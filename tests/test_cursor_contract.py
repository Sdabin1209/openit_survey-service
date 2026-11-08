"""알림 커서·SSE Last-Event-ID 는 계약(HttpQueries.Alerts.cursor·HttpWire.EventId: ASCII 숫자 1~18자리) 검증을 지난 뒤에만 int() 가 된다(5.0.0, INTERFACES I7).
왜: str.isdigit() 는 비ASCII 숫자('١٢٣')와 끝없는 자릿수를 받아 int() 가 값을 잘못 읽거나 BIGINT 를 넘겼다(500 경로)."""
import urllib.error
import urllib.request

import pytest

from .test_staff_alerts import AlertRig, STAFF
from .test_sse_c2 import live  # noqa: F401  (fixture)
from .timing import HANG_GUARD_SECONDS

BAD = ["١٢٣", "²", "1" * 19, "1" * 5000, "12 ", "-1", "1.5", "%0A1"]


@pytest.fixture
def ar(tmp_path):
    r = AlertRig(tmp_path)
    yield r
    r.store.close()


@pytest.mark.parametrize("bad", BAD)
def test_alerts_cursor_outside_the_contract_is_400_not_500(ar, bad):
    code, body, _ = ar.call("GET", "/v1/staff/alerts?cursor=" + bad, auth=STAFF)
    assert code == 400 and body["code"] == "INVALID_RECORD"


@pytest.mark.parametrize("good", ["1", "123456789012345678"])
def test_alerts_cursor_inside_the_contract_is_accepted(ar, good):
    assert ar.call("GET", "/v1/staff/alerts?cursor=" + good, auth=STAFF)[0] == 200


@pytest.mark.parametrize("bad", ["1" * 19, "1" * 5000, "abc", "\xb2"])      # 헤더는 latin-1 바이트: 비ASCII 숫자는 이 범위의 \xb2(위첨자 2, isdigit() 참)로 시험한다
def test_last_event_id_outside_the_contract_is_400(live, bad):  # noqa: F811
    app, port = live()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/staff/alerts/stream", headers={"Authorization": "Bearer test-staff-token", "Last-Event-ID": bad})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=HANG_GUARD_SECONDS)
    assert e.value.code == 400


def test_reverting_to_isdigit_would_accept_unicode_digits():
    """변이 확인: 옛 검사(isdigit)는 이 값을 받아들였다 = 위 시험이 그 되돌림을 잡는다."""
    assert "١٢٣".isdigit() and ("1" * 19).isdigit()
