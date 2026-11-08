"""PUBLIC_API SSE(계약 3.4.4, 코드 단계): 이벤트별 data 스키마, X-Openit-Roles로 이벤트 거름, 최대 수명 뒤 정상 종료(RUNTIME R2.4), 동시 연결 상한 초과 429+Retry-After."""
import json
import queue
import threading
import urllib.error
import urllib.request

import pytest
from boundary_contracts import validator

from .timing import HANG_GUARD_SECONDS, wait_until
from .http_server_support import stop_server
from .test_staff_alerts import SECRET_TEXT, api, http_call, make_app, start_and_risk


class Stream:
    def __init__(self, port, roles=None, token="test-staff-token"):
        h = {"Authorization": f"Bearer {token}"}
        if roles is not None:
            h["X-Openit-Roles"] = roles
        self.resp = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/staff/alerts/stream", headers=h), timeout=HANG_GUARD_SECONDS)
        self.events, self.ended = queue.Queue(), threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        ev = {}
        try:
            for raw in self.resp:
                line = raw.decode().rstrip("\n")
                if line == "":
                    if "data" in ev:
                        self.events.put((ev.get("event"), json.loads(ev["data"])))
                    ev = {}
                elif ":" in line and not line.startswith(":"):
                    k, _, v = line.partition(":")
                    ev[k] = v.strip()
        except Exception:
            pass
        self.ended.set()

    def names(self, n_wait=0):
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out


@pytest.fixture
def live(tmp_path, monkeypatch):
    holder = {}

    def make(extra=None):
        app, httpd, port = make_app(tmp_path, monkeypatch, extra=extra)
        holder.update(app=app, httpd=httpd)
        return app, port
    yield make
    if holder:
        holder["app"].draining.set()
        stop_server(holder["httpd"]); holder["app"].stop()


def test_over_the_connection_limit_is_429_with_retry_after(live):
    app, port = live({"SURVEY_MAX_SSE_CONNECTIONS": "1"})
    s1 = Stream(port)
    assert wait_until(lambda: app.alerts.hub.count() >= 1)
    with pytest.raises(urllib.error.HTTPError) as e:
        Stream(port)
    assert e.value.code == 429 and e.value.headers["Retry-After"] and json.loads(e.value.read())["code"] == "UNAVAILABLE"
    s1.resp.close()


def test_stream_ends_normally_after_the_maximum_lifetime(live):
    app, port = live({"SURVEY_SSE_MAX_SECONDS": "1"})
    s = Stream(port)
    assert s.ended.wait(HANG_GUARD_SECONDS)                    # 정상 종료(EOF): 연결을 끊는 오류가 아니다
    assert wait_until(lambda: app.alerts.hub.count() == 0)      # 구독도 풀렸다


def test_events_are_filtered_by_roles_and_data_match_contract_schemas(live):
    app, port = live()
    staff, clin, both = Stream(port, roles="staff"), Stream(port, roles="clinician"), Stream(port, roles="staff,clinician")
    assert wait_until(lambda: app.alerts.hub.count() >= 3)
    assert start_and_risk(port, "K1", "t-1")[0] == 200
    app._result_ready("K1", "ENDED_COMPLETE")
    want = {"staff": {"alert"}, "clin": {"result_ready"}, "both": {"alert", "result_ready"}}
    got = {"staff": [], "clin": [], "both": []}
    streams = {"staff": staff, "clin": clin, "both": both}

    def have_all():
        for k, st in streams.items():
            got[k] += st.names()
        return all(want[k] <= {n for n, _ in got[k]} for k in want)
    assert wait_until(have_all)                                 # 자기 몫의 이벤트가 모두 올 때까지(고정 sleep 없음)
    for k, st in streams.items():
        got[k] += st.names()
    kinds = {k: sorted({n for n, _ in v}) for k, v in got.items()}
    assert kinds["staff"] == ["alert", "heartbeat"] and kinds["clin"] == ["heartbeat", "result_ready"] and kinds["both"] == ["alert", "heartbeat", "result_ready"]
    schema = {"alert": "StaffAlert", "result_ready": "ResultReadyEvent", "heartbeat": "HeartbeatEvent"}
    for v in got.values():
        for name, data in v:
            assert list(validator(schema[name]).iter_errors(data)) == [], (name, data)
    assert SECRET_TEXT not in json.dumps(got, ensure_ascii=False)


def test_result_ready_for_a_server_issued_id_travels_the_real_sse_path_and_matches_the_contract(live):
    """실제 경로: 서버가 발급한 session_id(patch 없음)로 세션을 시작하고 POST /end 로 끝내면 /v1/staff/alerts/stream 에 result_ready 가 오고, 그 data 가 계약 ResultReadyEvent 를 통과한다(ADR-0066)."""
    app, port = live()
    clin = Stream(port, roles="clinician")
    assert wait_until(lambda: app.alerts.hub.count() >= 1)
    code, fi = api(port, "POST", "/v1/sessions", {"kind": "SESSION_START"})
    assert code == 200
    sid = fi["session_id"]
    code = api(port, "POST", f"/v1/sessions/{sid}/end", {"session_id": sid, "kind": "SESSION_END"})[0]
    assert code == 200
    got = []
    assert wait_until(lambda: (got.extend(clin.names()) or any(n == "result_ready" for n, _ in got)))
    data = next(d for n, d in got if n == "result_ready")
    assert data["session_id"] == sid and list(validator("ResultReadyEvent").iter_errors(data)) == []
