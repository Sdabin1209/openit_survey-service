"""CR-H001·CR-H002 (설치된 계약 패키지 기준): 직원 호출 재알림·실패 기록·정지 사유. SQLite와 일회용 PostgreSQL(TEST_DATABASE_URL) 모두.
권한 부정, 경로 우회, 멱등, 간격·상한, 크래시 구간, 계약 스키마 적합성을 본다."""
import json

import pytest
from boundary_contracts import validator

from survey_service.components.alert_service import AlertHub, AlertService, AlertStaffConsole
from survey_service.components.alert_book import AlertStore, PgAlertStore
from survey_service.components.link_public_api import PublicApi
from survey_service.components.session_store import StoreError

from .timing import HANG_GUARD_SECONDS, fake_clock
from .e2e_support import RISK_OUT, Rig, ScriptedRisk, ext, val
from .pg_support import pg_link, pg_url

KIOSK, STAFF, DEV_A, DEV_B = "Bearer kiosk-tok-0123456789", "Bearer staff-tok-0123456789", "Bearer dev-a-tok-0123456789", "Bearer dev-b-tok-0123456789"
RISK_TEXT = "숨이 안 쉬어져요"
REASK = ext("REASK", "Q1.D01", val("Q1.D01.on_medication", None, "MISSING"), reason="MISSING", targets=["Q1.D01.on_medication"])


def check(name, rec):
    errs = list(validator(name).iter_errors(rec))
    assert not errs, (name, errs[0].message, rec)


class SRig:
    def __init__(self, store, t, devices=False, interval=30, cap=10, risk=True, **orch_kw):
        self.t = t
        self.rig = Rig(risk=risk, **orch_kw)
        self.store = store
        self.hub = AlertHub()
        self.service = AlertService(store, self.hub, None, push_timeout=HANG_GUARD_SECONDS, retry_min_interval_s=interval, retry_max_per_session=cap, monotonic=fake_clock())
        self.rig.orch.staff_console = AlertStaffConsole(self.service)
        kw = {"device_tokens": {"dev-a": DEV_A[7:], "dev-b": DEV_B[7:]}} if devices else {}
        self.api = PublicApi(self.rig.orch, KIOSK[7:], staff_token=STAFF[7:], alerts=self.service, result_token="res-tok-0123456789",
                             risk_token="risk-tok-0123456789", state_poll_min_interval=0, **kw)

    def call(self, method, path, body=None, auth=KIOSK):
        raw = json.dumps(body).encode() if body is not None else b""
        return self.api.handle(method, path, auth, raw, user="staff-1")

    def start(self, sid="K1", auth=KIOSK):
        return self.call("POST", "/v1/sessions", {"session_id": sid, "kind": "SESSION_START"}, auth=auth)

    def risk(self, sid="K1", tid="t-risk", auth=KIOSK):
        self.rig.model.queue.append(RISK_OUT)
        return self.call("POST", f"/v1/sessions/{sid}/turns", {"session_id": sid, "kind": "UTTERANCE", "utterance": RISK_TEXT, "turn_id": tid}, auth=auth)

    def retry(self, tid, sid="K1", auth=KIOSK):
        return self.call("POST", f"/v1/sessions/{sid}/staff-call/retry", {"turn_id": tid}, auth=auth)

    def report(self, tid, outcome="STAFF_NOTIFIED_MANUALLY", sid="K1", auth=KIOSK):
        return self.call("POST", f"/v1/sessions/{sid}/staff-call/report", {"outcome": outcome, "turn_id": tid}, auth=auth)

    def state(self, sid="K1", auth=KIOSK):
        return self.call("GET", f"/v1/sessions/{sid}/state", auth=auth)


@pytest.fixture(params=["sqlite", "postgres"])
def mk(request, tmp_path):
    """(SRig 만드는 함수). 시험마다 새 저장소와 조작 가능한 시계."""
    t = [1_700_000_000.0]
    stores = []

    def make(**kw):
        s = PgAlertStore(pg_url(request), **pg_link(), clock=lambda: t[0]) if request.param == "postgres" else AlertStore(str(tmp_path / "a.db"), clock=lambda: t[0])
        stores.append(s)
        r = SRig(s, t, **kw)
        if not kw.get("devices"):
            r.start()
        return r
    yield make
    for s in stores:
        s.close()


def failed_risk(r):
    """구독자 없이 위험 정지: 알림은 저장되고 DELIVERY_FAILED."""
    code, fi, _ = r.risk()
    assert code == 200 and fi["turn_failure_signal"]["staff_alert"] == "DELIVERY_FAILED"
    return fi


# ---------------------------------------------------------------- 재알림
def test_retry_redelivers_same_alert_without_creating_a_new_one(mk):
    r = mk()
    failed_risk(r)
    alerts0 = r.store.list(None, 10, None)[0]
    assert len(alerts0) == 1 and alerts0[0]["delivery_attempts"] == 1 and alerts0[0]["last_delivery_status"] == "DELIVERY_FAILED"
    code, body, _ = r.retry("rt-1")                      # 구독자가 없으니 이번에도 못 닿는다: 200 + DELIVERY_FAILED
    assert code == 200 and body == {"alert_id": alerts0[0]["alert_id"], "staff_alert": "DELIVERY_FAILED", "delivery_attempts": 2, "retry_after_s": 30}
    check("StaffCallRetryResult", body)
    q = r.hub.subscribe()                                # 콘솔이 붙었다
    r.t[0] += 31
    code, body, _ = r.retry("rt-2")
    assert code == 200 and body["staff_alert"] == "DELIVERED" and body["delivery_attempts"] == 3 and body["alert_id"] == alerts0[0]["alert_id"]
    ev = q.get_nowait()                                   # SSE alert 이벤트, 같은 alert_id, 새 이벤트 순번
    assert ev["type"] == "alert" and ev["alert"]["alert_id"] == body["alert_id"] and ev["alert"]["delivery_attempts"] == 3
    alerts1 = r.store.list(None, 10, None)[0]
    assert len(alerts1) == 1 and alerts1[0]["last_delivery_status"] == "DELIVERED" and alerts1[0]["delivery_attempts"] == 3
    check("StaffAlert", alerts1[0])
    assert r.service.stats["staff_call_retries"] == 2 and r.service.stats["staff_call_retry_delivered"] == 1


def test_same_turn_id_is_idempotent_and_skips_interval_check(mk):
    r = mk()
    failed_risk(r)
    first = r.retry("rt-1")
    r.hub.subscribe()                                    # 상태가 바뀌어도 같은 turn_id는 처음 응답 그대로
    assert r.retry("rt-1") == first and r.retry("rt-1") == first
    assert r.store.list(None, 10, None)[0][0]["delivery_attempts"] == 2
    assert r.service.stats["staff_call_retry_replayed"] == 2


def test_interval_and_per_session_cap(mk):
    r = mk(interval=30, cap=2)
    failed_risk(r)
    assert r.retry("rt-1")[0] == 200                     # 첫 재알림은 바로 된다(간격은 재알림 사이)
    code, body, h = r.retry("rt-2")
    assert code == 429 and body["code"] == "UNAVAILABLE" and body["details"]["reason"] == "INTERVAL" and h["Retry-After"] == "30"
    r.t[0] += 10
    assert r.retry("rt-2")[2]["Retry-After"] == "20"
    r.t[0] += 20
    assert r.retry("rt-2")[0] == 200
    r.t[0] += 31
    code, body, h = r.retry("rt-3")                      # 상한 2회
    assert code == 429 and body["details"]["reason"] == "RETRY_LIMIT" and "Retry-After" not in h      # 기다려도 풀리지 않는다
    assert r.retry("rt-1")[0] == 200                     # 이미 처리한 turn_id의 재전송은 상한과 무관하게 같은 응답
    assert r.service.stats["staff_call_retry_rejected_interval"] == 2 and r.service.stats["staff_call_retry_rejected_limit"] == 1


def test_no_open_alert_is_409_and_resolved_alert_is_not_resent(mk):
    r = mk()
    code, body, _ = r.retry("rt-0")
    assert code == 409 and body["details"]["reason"] == "NO_OPEN_ALERT"
    failed_risk(r)
    r.api.alerts.resolve("K1", "RESUME", "dr")
    code, body, _ = r.retry("rt-1")
    assert code == 409 and body["details"]["reason"] == "NO_OPEN_ALERT"


def test_crash_between_begin_and_finish_redelivers_same_alert_without_counting_again(mk, monkeypatch):
    """begin_retry(시도 횟수 +1, turn_id 기록, 새 이벤트) 뒤 finish_retry(멱등 기록) 전에 죽은 구간(ADR-0021): 같은 turn_id 재전송은 간격·상한을 건너뛰고
    알림을 새로 만들지 않고 같은 alert_id로 다시 전달하며(최소 한 번) 시도 횟수는 더 늘지 않고 응답을 돌려준다."""
    r = mk()
    failed_risk(r)
    q = r.hub.subscribe()
    real = r.store.finish_retry
    monkeypatch.setattr(r.store, "finish_retry", lambda *a, **k: (_ for _ in ()).throw(StoreError("crash")))
    assert r.retry("rt-1")[0] == 503
    assert r.store.list(None, 10, None)[0][0]["delivery_attempts"] == 2      # 시도는 셌다
    assert q.get_nowait()["type"] == "alert"
    monkeypatch.setattr(r.store, "finish_retry", real)
    code, body, _ = r.retry("rt-1")                      # 시간이 지나지 않아도(간격 안) 같은 turn_id는 막히지 않는다
    items = r.store.list(None, 10, None)[0]
    assert code == 200 and len(items) == 1 and body["alert_id"] == items[0]["alert_id"] == "al-1"
    assert body["delivery_attempts"] == 2 and body["staff_alert"] == "DELIVERED" and items[0]["delivery_attempts"] == 2
    assert q.get_nowait()["alert"]["alert_id"] == "al-1"      # 다시 전달했다
    assert r.retry("rt-1")[1] == body                    # 이제 기록됐으니 같은 응답
    assert r.retry("rt-other")[0] == 429                 # 다른 turn_id는 간격 검사를 받는다


def test_resolve_and_retry_never_emit_an_open_alert_event_after_resolved(mk):
    """M-1: 직원 해결과 재알림이 겹쳐도 해결된 알림에 'alert' 이벤트가 나가지 않는다. 저장소 수준의 원자 조건과 두 스레드 경합 둘 다."""
    import threading
    r = mk()
    failed_risk(r)
    r.store.resolve("K1", "RESUME", "s1")                # 서비스 잠금을 거치지 않은 해결 -> 1단계의 원자 조건이 막는다
    from survey_service.wiring.errors import StaffCallRejected
    with pytest.raises(StaffCallRejected) as e:
        r.store.begin_retry(1, "rt-x")
    assert e.value.kind == "NO_OPEN_ALERT"
    for i in range(12):                                   # 경합: 각 라운드는 새 세션
        sid = f"R{i}"
        r.start(sid)
        r.risk(sid, f"tr{i}")
        q = r.hub.subscribe()
        bar = threading.Barrier(2)
        res = []

        def do_retry():
            bar.wait(); res.append(r.retry(f"x{i}", sid)[0])

        def do_resolve():
            bar.wait(); r.service.resolve(sid, "RESUME", "s1")
        ts = [threading.Thread(target=do_retry), threading.Thread(target=do_resolve)]
        [t.start() for t in ts]; [t.join() for t in ts]
        evs = []
        while not q.empty():
            evs.append(q.get_nowait())
        r.hub.unsubscribe(q)
        kinds = [(e["type"], e["alert"]["status"]) for e in evs if e["alert"]["session_id"] == sid]
        assert res[0] in (200, 409), res
        if ("resolved", "RESOLVED") in kinds:
            assert not any(k == ("alert", "OPEN") for k in kinds[kinds.index(("resolved", "RESOLVED")):]), kinds     # resolved 뒤에 OPEN alert 없음


def test_clock_going_backwards_does_not_extend_wait(mk):
    r = mk(interval=30)
    failed_risk(r)
    assert r.retry("rt-1")[0] == 200
    r.t[0] -= 3600                                        # 시계가 한 시간 거꾸로
    code, body, h = r.retry("rt-2")
    assert code == 429 and int(h["Retry-After"]) <= 30


# ---------------------------------------------------------------- 실패 기록
def test_report_records_on_alert_and_in_audit_log_and_is_idempotent(mk):
    r = mk()
    failed_risk(r)
    code, body, _ = r.report("rp-1", "NOT_NOTIFIED")
    assert code == 200 and body["manual_notice_outcome"] == "NOT_NOTIFIED" and body["manual_notice_reported_at"].endswith("Z") and body["status"] == "OPEN"
    check("StaffAlert", body)
    assert r.report("rp-1", "NOT_NOTIFIED") == (200, body, {})
    code, b2, _ = r.report("rp-1", "STAFF_NOTIFIED_MANUALLY")
    assert code == 409 and b2["code"] == "CONFLICT"
    code, b3, _ = r.report("rp-2", "STAFF_NOTIFIED_MANUALLY")      # 새 보고는 최신 값으로 덮는다
    assert code == 200 and b3["manual_notice_outcome"] == "STAFF_NOTIFIED_MANUALLY"
    log = [e for e in r.store.access_log("K1") if e["outcome"] == "STAFF_CALL_REPORT"]
    assert len(log) == 2 and log[0]["role"] == "kiosk"            # 감사 기록: 토큰 값·발화 없음
    assert r.state()[1]["state"] == "PAUSED_RISK"                  # 세션은 그대로 정지
    code, view, _ = r.call("GET", "/v1/staff/sessions/K1", auth=STAFF)
    assert view["alerts"][0]["manual_notice_outcome"] == "STAFF_NOTIFIED_MANUALLY" and view["alerts"][0]["delivery_attempts"] == 1


def test_report_on_resolved_alert_is_recorded_and_announced_and_capped(mk):
    r = mk()
    failed_risk(r)
    r.service.resolve("K1", "RESUME", "dr")
    q = r.hub.subscribe()
    code, body, _ = r.report("rp-1", "NOT_NOTIFIED")
    assert code == 200 and body["status"] == "RESOLVED" and body["manual_notice_outcome"] == "NOT_NOTIFIED"      # 사후 기록
    ev = q.get_nowait()
    assert ev["type"] == "alert" and ev["alert"]["manual_notice_outcome"] == "NOT_NOTIFIED"      # 콘솔이 안다
    r.service.report_max_per_session = 2
    assert r.report("rp-2")[0] == 200
    code, body, h = r.report("rp-3")
    assert code == 429 and body["details"]["reason"] == "RETRY_LIMIT" and "Retry-After" not in h
    assert r.report("rp-1", "NOT_NOTIFIED")[0] == 200       # 이미 처리한 turn_id는 상한과 무관


def test_report_without_any_alert_is_409(mk):
    r = mk()
    assert r.report("rp-1")[0] == 409


# ---------------------------------------------------------------- 권한 부정, 경로 우회, 잘못된 본문
def test_authorization_and_path_bypass(mk):
    r = mk(devices=True)
    r.start("K1", auth=DEV_A)
    r.start("K2", auth=DEV_B)
    r.risk("K1", auth=DEV_A)
    r.risk("K2", "t-risk2", auth=DEV_B)
    assert r.retry("x1", auth=DEV_A)[0] == 200
    assert r.retry("x2", auth=DEV_B)[0] == 404 and r.retry("x2", auth=DEV_B, sid="K1")[0] == 404      # 다른 기기: 404
    assert r.report("x2", auth=DEV_B)[0] == 404
    assert r.retry("x3", auth=KIOSK)[0] == 404 and r.report("x3", auth=KIOSK)[0] == 404              # 공유 토큰도 기기 소유 세션에는 못 닿는다
    for auth, want in ((STAFF, 403), ("Bearer res-tok-0123456789", 403), ("Bearer risk-tok-0123456789", 403), ("Bearer wrong", 401), ("", 401)):
        assert r.retry("y1", auth=auth, sid="K1")[0] == want and r.report("y1", auth=auth, sid="K1")[0] == want, auth
    # 경로 우회: 정규화·인코딩·접미사·메서드로 다른 세션/경로에 닿지 못한다
    for path in ("/v1/sessions/K1/../K2/staff-call/retry", "/v1/sessions/K1/staff-call/retry/", "/v1/sessions/K1/staff-call/retry/extra",
                 "/v1/sessions//staff-call/retry", "/v1/staff/sessions/K1/staff-call/retry", "/v1/sessions/K1/staff-call", "/v1/sessions/K1/STAFF-CALL/retry"):
        assert r.call("POST", path, {"turn_id": "z"}, auth=DEV_B)[0] in (404, 400), path
    for path in ("/v1/sessions/K1%2FK2/staff-call/retry", "/v1/sessions/K1%00/staff-call/retry", "/v1/sessions/%2e%2e/staff-call/retry"):
        assert r.call("POST", path, {"turn_id": "z"}, auth=DEV_A)[0] in (400, 404), path      # 형식 위반(기기 확인이 먼저면 404): 어느 쪽이든 아무것도 바꾸지 않는다
    assert r.call("GET", "/v1/sessions/K1/staff-call/retry", auth=DEV_A)[0] == 405 and r.call("PUT", "/v1/sessions/K1/staff-call/retry", {"turn_id": "z"}, auth=DEV_A)[0] == 405      # 알려진 경로의 다른 메서드는 405(openit_link 규약, 옛 동작은 404)
    assert r.retry("q1", sid="NOPE", auth=DEV_A)[0] == 404                                    # 없는 세션


def test_bad_bodies_are_400(mk):
    r = mk()
    failed_risk(r)
    p = "/v1/sessions/K1/staff-call/"
    bad = [("retry", {}), ("retry", {"turn_id": ""}), ("retry", {"turn_id": "x" * 65}), ("retry", {"turn_id": "a", "utterance": "아파요"}),
           ("retry", {"turn_id": 5}), ("retry", {"turn_id": "start"}), ("retry", {"turn_id": "bad id"}),
           ("report", {"turn_id": "a"}), ("report", {"turn_id": "a", "outcome": "MAYBE"}), ("report", {"turn_id": "a", "outcome": "NOT_NOTIFIED", "note": "자유 문장"})]
    for act, body in bad:
        assert r.call("POST", p + act, body)[0] == 400, (act, body)
    assert r.call("POST", p + "retry", None)[0] == 400 and r.call("POST", p + "retry", [1])[0] == 400
    assert r.retry("rt-1\n")[0] == 400 and r.report("rp-1\n")[0] == 400 and r.call("POST", "/v1/sessions/K1\n/staff-call/retry", {"turn_id": "a"})[0] == 400      # 끝의 개행
    assert r.api.handle("POST", p + "retry", KIOSK, b'{"turn_id":"d1","turn_id":"d2"}')[0] == 400        # 중복 키
    assert r.store.list(None, 10, None)[0][0]["delivery_attempts"] == 1        # 거절된 요청은 아무것도 바꾸지 않았다


# ---------------------------------------------------------------- 정지 사유 (CR-H002)
def test_pause_reason_risk_in_state_signal_alert_and_staff_view(mk):
    r = mk()
    code, fi, _ = r.risk()
    sig = fi["turn_failure_signal"]
    assert sig["pause_reason"] == "RISK"
    code, st, _ = r.state()
    assert st["pause_reason"] == "RISK"
    check("SessionState", st)
    a = r.store.list(None, 10, None)[0][0]
    assert a["pause_reason"] == "RISK"
    check("StaffAlert", a)
    view = r.call("GET", "/v1/staff/sessions/K1", auth=STAFF)[1]
    assert view["pause_reason"] == "RISK" and view["alerts"][0]["pause_reason"] == "RISK"
    r.call("POST", "/v1/sessions/K1/intervention", {"session_id": "K1", "decision": "RESUME", "turn_id": "i-1"}, auth=STAFF)
    st = r.state()[1]
    assert st["state"] == "ACTIVE" and "pause_reason" not in st       # 정지가 아니면 생략


def test_pause_reason_reask_limit_and_external_call_staff(mk):
    r = mk(risk=False, max_consecutive_reask=2)
    for i in range(3):
        r.rig.model.queue.append(REASK)
        code, fi, _ = r.call("POST", "/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "음", "turn_id": f"t{i}"})
    sig = fi["turn_failure_signal"]
    assert sig["pause_reason"] == "REASK_LIMIT" and sig["staff_alert"] == "DELIVERY_FAILED"
    st = r.state()[1]
    assert st["state"] == "PAUSED_RISK" and st["pause_reason"] == "REASK_LIMIT"       # PAUSED_RISK 이름 유지, 사유로 구분
    a = r.store.list(None, 10, None)[0][0]
    assert a["kind"] == "CALL_STAFF" and a["pause_reason"] == "REASK_LIMIT"
    assert r.call("GET", "/v1/staff/sessions/K1", auth=STAFF)[1]["pause_reason"] == "REASK_LIMIT"
    r.call("POST", "/v1/sessions/K1/intervention", {"session_id": "K1", "decision": "RESUME", "turn_id": "i-1"}, auth=STAFF)
    # 외부 감지기의 CALL_STAFF 신호는 위험 판정이 아니라 호출이므로 OTHER
    code, res, _ = r.call("POST", "/v1/sessions/K1/risk-signals", {"signal_id": "s1", "source": "vitals", "code": "HR", "severity": "CALL_STAFF"}, auth="Bearer risk-tok-0123456789")
    assert code in (200, 201), res
    assert r.state()[1]["pause_reason"] == "OTHER"
    code, res, _ = r.call("POST", "/v1/sessions/K1/intervention", {"session_id": "K1", "decision": "RESUME", "turn_id": "i-2"}, auth=STAFF)
    code, res, _ = r.call("POST", "/v1/sessions/K1/risk-signals", {"signal_id": "s2", "source": "vitals", "code": "HR", "severity": "RISK"}, auth="Bearer risk-tok-0123456789")
    assert r.state()[1]["pause_reason"] == "RISK"


def test_pause_reason_comes_from_current_pause_even_when_alert_store_write_fails(mk, monkeypatch):
    """M-2: RISK 정지 -> RESUME -> 알림 저장 실패 상태에서 되묻기 한도 정지. 이전 정지의 RISK가 남지 않고, 상태·직원 조회·턴 신호가 일치한다."""
    r = mk(risk=True, max_consecutive_reask=2)
    failed_risk(r)
    r.call("POST", "/v1/sessions/K1/intervention", {"session_id": "K1", "decision": "RESUME", "turn_id": "i-1"}, auth=STAFF)
    monkeypatch.setattr(r.store, "create", lambda *a, **k: (_ for _ in ()).throw(StoreError("disk")))
    for i in range(3):
        r.rig.model.queue.append(REASK)
        code, fi, _ = r.call("POST", "/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "음 잘 모르겠어요", "turn_id": f"q{i}"})
    assert fi["turn_failure_signal"]["pause_reason"] == "REASK_LIMIT"
    assert r.state()[1]["pause_reason"] == "REASK_LIMIT"
    assert r.call("GET", "/v1/staff/sessions/K1", auth=STAFF)[1]["pause_reason"] == "REASK_LIMIT"


def test_all_four_surfaces_agree_on_pause_reason(mk):
    r = mk()
    fi = r.risk()[1]
    assert (fi["turn_failure_signal"]["pause_reason"], r.state()[1]["pause_reason"], r.store.list(None, 10, None)[0][0]["pause_reason"],
            r.call("GET", "/v1/staff/sessions/K1", auth=STAFF)[1]["pause_reason"]) == ("RISK",) * 4


def test_readyz_stats_expose_retry_counts(mk):
    r = mk(interval=7, cap=4)
    failed_risk(r)
    r.retry("rt-1"); r.retry("rt-2"); r.report("rp-1", "NOT_NOTIFIED")
    s = r.service.status()
    assert s["staff_call_retries"] == 1 and s["staff_call_retry_rejected_interval"] == 1 and s["staff_call_reports"] == 1 and s["staff_call_reports_not_notified"] == 1
    assert s["retry_min_interval_s"] == 7 and s["retry_max_per_session"] == 4
