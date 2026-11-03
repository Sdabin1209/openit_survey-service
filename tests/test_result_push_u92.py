"""U92(계약 3.4.0): 푸시 구독 topics(alerts=staff, results=clinician)와 결과 도착 웹 푸시 RESULT_READY, 발송 실패 재시도 (ADR-0059)."""
import json

import pytest
from boundary_contracts import validator

from survey_service.components.alert_queue import AlertQueue

from .test_results_signals_retention import AUTH, RESULT, STAFF, env  # noqa: F401
from .test_service import running, settings  # noqa: F401,F811


class Push:
    def __init__(self):
        self.sent, self.outcome = [], "ACCEPTED"

    def send(self, subscription, payload):
        self.sent.append((dict(subscription), json.loads(payload)))
        return self.outcome


def sub(ep, **extra):
    return {"endpoint": f"https://push.example.com/{ep}", "keys": {"p256dh": "k", "auth": "a"}, **extra}


@pytest.fixture
def rig(env, tmp_path):
    clock = [1000.0]
    env.service.push = Push()
    env.service.queue = AlertQueue(str(tmp_path / "q.db"), clock=lambda: clock[0])
    yield env, env.service, clock
    env.service.queue.close()


def add(env, body, auth):
    return env.call("POST", "/v1/staff/push-subscriptions", body, auth=auth)


def test_results_subscriber_gets_one_valid_push_and_alerts_only_subscriber_gets_none(rig):
    env, svc, _ = rig
    assert add(env, sub("doc", topics=["results"]), RESULT)[0] == 201
    assert add(env, sub("nurse"), STAFF)[0] == 201                       # topics 없음 = alerts
    assert svc.push_result_ready("K1") == (1, 1)
    assert len(svc.push.sent) == 1
    s, body = svc.push.sent[0]
    assert s["endpoint"].endswith("/doc") and "topics" not in s          # 주제 표시는 푸시 서비스에 보내지 않는다
    assert body == {"session_id": "K1", "kind": "RESULT_READY"} and not list(validator("StaffPushPayload").iter_errors(body))


def test_alerts_only_subscribers_get_no_result_push(rig):
    env, svc, _ = rig
    add(env, sub("nurse", topics=["alerts"]), STAFF)
    assert svc.push_result_ready("K1") == (0, 0) and svc.push.sent == []


def test_alert_push_does_not_go_to_results_only_subscribers(rig):
    env, svc, _ = rig
    add(env, sub("doc", topics=["results"]), RESULT)
    assert svc._push({"alert": {"alert_id": "al_1", "session_id": "K1", "kind": "RISK_STOP"}}) == 0 and svc.push.sent == []


def test_staff_cannot_register_results_and_clinician_cannot_register_alerts(rig):
    env, _, _ = rig
    assert add(env, sub("x", topics=["results"]), STAFF)[0] == 403
    assert add(env, sub("y", topics=["alerts"]), RESULT)[0] == 403
    assert add(env, sub("z"), RESULT)[0] == 403                           # 기본 alerts는 staff 몫
    assert add(env, sub("w", topics=["alerts", "results"]), STAFF)[0] == 403
    assert add(env, sub("v", topics=["nope"]), STAFF)[0] == 400
    assert add(env, sub("u"), AUTH)[0] in (401, 403)
    assert env.service.store.count_subscriptions() == 0


def test_failed_push_is_retried_until_accepted(rig):
    env, svc, clock = rig
    add(env, sub("doc", topics=["results"]), RESULT)
    svc.push.outcome = "FAILED"
    assert svc.push_result_ready("K1") == (1, 0) and svc.queue.counts()["pending"] == 1
    clock[0] += 1
    assert svc.drain_queue(clock[0]) == 0 and svc.queue.counts()["pending"] == 1      # 아직 실패: 백오프
    svc.push.outcome = "ACCEPTED"
    n = len(svc.push.sent)
    clock[0] += 100000
    assert svc.drain_queue(clock[0]) == 1 and svc.queue.counts()["pending"] == 0 and len(svc.push.sent) == n + 1
    assert svc.push.sent[-1][1] == {"session_id": "K1", "kind": "RESULT_READY"}


def test_retry_gives_up_after_the_attempt_limit(rig):
    env, svc, clock = rig
    add(env, sub("doc", topics=["results"]), RESULT)
    svc.push.outcome = "FAILED"
    svc.push_result_ready("K1")
    for _ in range(svc.RESULT_PUSH_MAX_ATTEMPTS + 1):
        clock[0] += 1000
        svc.drain_queue(clock[0])
    assert svc.queue.counts()["pending"] == 0


def test_clinician_exposure_declares_push_paths():
    from pathlib import Path
    d = json.loads((Path(__file__).resolve().parent.parent / "deploy-exposure.json").read_text(encoding="utf-8"))
    rows = [r for r in d["routes"] if r["auth"].get("roles") == ["clinician"] and any("push" in p for p in r["paths"])]
    assert {p for r in rows for p in r["paths"]} == {"/v1/staff/push-config", "/v1/staff/push-subscriptions", "/v1/staff/push-subscriptions/unsubscribe"}
    assert all(r["upstream_token"] == "survey-result-reader" for r in rows)


def test_app_result_ready_also_triggers_the_result_push(running, monkeypatch):
    import threading
    app = running[0]
    got, done = [], threading.Event()
    app.alerts.push_result_ready = lambda sid: (got.append(sid), done.set())
    app._result_ready("K1", "ENDED_COMPLETE")
    assert done.wait(5) and got == ["K1"]


# ---------------------------------------------------------------- S7: X-Openit-Roles (계약 3.4.0 CONVENTIONS 6절)
def add_roles(env, body, auth, roles):
    return env.api.handle("POST", "/v1/staff/push-subscriptions", auth, json.dumps(body).encode(), user="u1", roles=roles)


def test_account_with_staff_and_clinician_roles_can_subscribe_both_topics_with_one_token(rig):
    env, svc, _ = rig
    for tok in (STAFF, RESULT):                     # 게이트웨이는 토큰 하나만 붙인다: 어느 토큰이어도 된다
        assert add_roles(env, sub(f"both{tok[-1]}", topics=["alerts", "results"]), tok, "staff,clinician")[0] == 201
    assert add_roles(env, sub("r", topics=["results"]), STAFF, "staff, clinician")[0] == 201
    assert sorted(s["topics"] for s in svc.store.subscriptions())[0] == ["alerts", "results"]


def test_clinician_only_account_cannot_subscribe_alerts_and_staff_only_cannot_results(rig):
    env, _, _ = rig
    assert add_roles(env, sub("a"), RESULT, "clinician")[0] == 403                          # 기본 alerts
    assert add_roles(env, sub("b", topics=["alerts"]), STAFF, "clinician")[0] == 403       # 토큰이 staff여도 역할이 우선
    assert add_roles(env, sub("c", topics=["results"]), RESULT, "staff")[0] == 403
    assert add_roles(env, sub("d", topics=["results"]), RESULT, "clinician")[0] == 201
    assert add_roles(env, sub("e", topics=["alerts", "results"]), STAFF, "clinician")[0] == 403
    assert add_roles(env, sub("f"), STAFF, "nonsense,admin")[0] == 403                     # 고정 목록 밖 이름은 무시, 역할 없음


def test_without_the_header_the_token_kind_decides_as_before(rig):
    env, _, _ = rig
    assert add(env, sub("x", topics=["results"]), RESULT)[0] == 201 and add(env, sub("y"), STAFF)[0] == 201
    assert add(env, sub("z", topics=["results"]), STAFF)[0] == 403


def test_forged_roles_header_on_a_non_gateway_token_is_ignored(rig):
    env, _, _ = rig
    # 키오스크 토큰은 push 경로의 역할이 아니다: 헤더가 clinician이어도 거절된다
    st, _, _ = add_roles(env, sub("k", topics=["results"]), AUTH, "clinician,staff")
    assert st in (401, 403)
    assert env.api.handle("GET", "/v1/staff/push-config", AUTH, b"", user="u1", roles="staff")[0] in (401, 403)
    assert env.service.store.count_subscriptions() == 0


def test_push_config_and_unsubscribe_accept_both_gateway_tokens(rig):
    env, _, _ = rig
    for tok in (STAFF, RESULT):
        assert env.call("GET", "/v1/staff/push-config", auth=tok)[0] == 200
    add(env, sub("u", topics=["results"]), RESULT)
    assert env.call("POST", "/v1/staff/push-subscriptions/unsubscribe", {"endpoint": sub("u")["endpoint"]}, auth=RESULT)[0] == 204
