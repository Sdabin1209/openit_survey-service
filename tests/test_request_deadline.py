"""직원 경로 요청 전체 마감(D120, 헤드 지시 U427): 여러 DB 문장을 읽는 직원 GET은 요청 마감(기본 9.0초, 주입 시계) 안에 응답하고,
넘기면 503 UNAVAILABLE이다. 각 문장은 남은 시간이 상한이다. 시험은 가짜 시계를 흘려 마감을 낸다(실제 시간을 재지 않는다)."""
import threading

from .test_staff_view_and_rules import RULES, ViewRig, converse
from .timing import HANG_GUARD_SECONDS, POLL_SECONDS, FakeClock


def _rig(tmp_path):
    vr = ViewRig(tmp_path, rules=RULES)
    converse(vr)
    clock = FakeClock()
    vr.rig.orch.clock = clock
    vr.rig.orch.request_deadline_seconds = 9.0
    return vr, clock


def _call_while_clock_runs(vr, clock, method, path, step, entered):
    res = {}
    th = threading.Thread(target=lambda: res.update(r=vr.call(method, path)), daemon=True)
    th.start()
    assert entered.wait(HANG_GUARD_SECONDS)       # 매달린 문장이 시작된 뒤에만 시계를 흘린다(마감 시각은 요청이 문장을 시작하기 전에 정해졌다)
    for _ in range(HANG_GUARD_SECONDS * 20):
        clock.advance(step)
        th.join(POLL_SECONDS)
        if not th.is_alive():
            break
    assert not th.is_alive()
    return res["r"]


def test_staff_session_view_returns_503_when_a_statement_hangs_past_the_request_deadline(tmp_path):
    vr, clock = _rig(tmp_path)
    gate, entered = threading.Event(), threading.Event()
    real = vr.store.timeline

    def hanging(sid):
        entered.set()
        gate.wait(HANG_GUARD_SECONDS)
        return real(sid)
    vr.store.timeline = hanging
    try:
        status, body, _ = _call_while_clock_runs(vr, clock, "GET", "/v1/staff/sessions/K1", 10.0, entered)
        assert entered.is_set() and status == 503 and body["code"] == "UNAVAILABLE"
    finally:
        gate.set()
        vr.store.close()


def test_alert_list_returns_503_when_the_read_hangs_and_200_when_it_does_not(tmp_path):
    vr, clock = _rig(tmp_path)
    assert vr.call("GET", "/v1/staff/alerts")[0] == 200
    gate, entered = threading.Event(), threading.Event()
    real = vr.store.list
    vr.store.list = lambda *a: (entered.set(), gate.wait(HANG_GUARD_SECONDS), real(*a))[2]
    try:
        status, body, _ = _call_while_clock_runs(vr, clock, "GET", "/v1/staff/alerts", 10.0, entered)
        assert status == 503 and body["code"] == "UNAVAILABLE"
    finally:
        gate.set()
        vr.store.close()


def test_the_budget_is_shared_by_the_statements_of_one_request(tmp_path):
    """문장마다 따로 9초를 주지 않는다: 첫 문장이 8초를 쓰면 다음 문장은 남은 1초가 상한이다."""
    vr, clock = _rig(tmp_path)
    orch = vr.rig.orch
    until = orch.request_until()
    assert orch.bounded(lambda: clock.advance(8.0) or "a", until) == "a"
    gate = threading.Event()
    th_res = {}

    def second():
        try:
            orch.bounded(lambda: gate.wait(HANG_GUARD_SECONDS), until)
        except orch.errors.StoreError as e:
            th_res["e"] = e
    th = threading.Thread(target=second, daemon=True)
    th.start()
    for _ in range(HANG_GUARD_SECONDS * 20):
        clock.advance(0.5)           # 남은 1초(9 - 8)를 넘기면 끝난다
        th.join(POLL_SECONDS)
        if not th.is_alive():
            break
    gate.set()
    vr.store.close()
    assert "e" in th_res and 8.0 < clock() - 1000.0 < 10.5
