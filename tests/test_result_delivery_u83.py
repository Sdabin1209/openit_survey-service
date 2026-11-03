"""U83: 결과 전달 - 확인 표시(ack) 멱등과 필드, 종료 때 result_ready 이벤트 1회, 읽기·ack 접근 기록(계정 포함), 노출 선언 (ADR-0056)."""
import json
from pathlib import Path

import jsonschema

from survey_service.server import _sse

from .test_results_signals_retention import AUTH, RESULT, RESULT_V, STAFF, env  # noqa: F401
from .test_service import running, settings  # noqa: F401,F811
from .sample_bank import sample_bank

ROOT = Path(__file__).resolve().parent.parent


def ack(env, sid="K1", user="dr.kim", auth=RESULT, body=None):
    return env.call("POST", f"/v1/results/{sid}/ack", {} if body is None else body, auth=auth, user=user)


def test_ack_sets_account_and_time_and_is_idempotent(env):
    env.start("K1")
    st, before, _ = env.call("GET", "/v1/results/K1", auth=RESULT)
    assert st == 200 and "acknowledged_by" not in before and "acknowledged_at" not in before
    st, first, _ = ack(env, user="dr.kim")
    assert st == 200 and first["acknowledged_by"] == "dr.kim" and first["acknowledged_at"].endswith("Z") and not list(RESULT_V.iter_errors(first))
    st, second, _ = ack(env, user="dr.lee")                       # 다른 계정이 다시 눌러도 처음 값이 남는다
    assert st == 200 and second["acknowledged_by"] == "dr.kim" and second["acknowledged_at"] == first["acknowledged_at"]
    st, read, _ = env.call("GET", "/v1/results/K1", auth=RESULT)
    assert read["acknowledged_by"] == "dr.kim"                    # 읽기에도 나온다
    assert env.call("GET", "/v1/results", auth=RESULT)[1]["items"][0]["acknowledged_by"] == "dr.kim"


def test_ack_requires_result_role_account_empty_body_and_known_session(env):
    env.start("K1")
    assert ack(env, auth=STAFF)[0] == 403 and ack(env, auth=AUTH)[0] == 403          # 다른 역할 토큰은 못 쓴다
    assert ack(env, user=None)[0] == 401                                              # 계정 없는 호출
    assert ack(env, body={"acknowledged_by": "x"})[0] == 400                          # 본문 {}만
    assert ack(env, sid="NOPE")[0] == 404
    assert "acknowledged_by" not in env.call("GET", "/v1/results/K1", auth=RESULT)[1]


def test_reads_and_acks_are_audited_with_the_account(env):
    env.start("K1")
    env.call("GET", "/v1/results/K1", auth=RESULT, user="dr.kim")
    ack(env, user="dr.kim")
    rows = [r for r in env.store.access_log("K1") if r["role"] == "result-reader"]
    assert [(r["outcome"], r["user_id"]) for r in rows] == [("ACK", "dr.kim"), ("OK", "dr.kim")] or [(r["outcome"], r["user_id"]) for r in rows] == [("OK", "dr.kim"), ("ACK", "dr.kim")]


def test_ack_is_refused_when_the_access_record_cannot_be_written(env):
    env.start("K1")
    def boom(*a, **k):
        from survey_service.wiring.errors import StoreError
        raise StoreError("disk")
    env.store.log_access_many = boom
    assert ack(env)[0] == 503
    assert env.store.ack_of("K1") is None                       # 기록 없이는 바꾸지 않는다


def test_result_ready_is_published_once_when_the_session_ends(running):
    app, reader, ingest, port = running
    reader.add_release("rel-1", sample_bank())
    reader.set_pointer("rel-1")
    app.start()
    q = app.alerts.hub.subscribe()
    app.orchestrator.handle_turn(turn_id="a1", frontend_message={"session_id": "S1", "kind": "SESSION_START"})
    assert q.empty()                                              # 시작은 결과가 아니다
    app.orchestrator.handle_turn(turn_id="e1", frontend_message={"session_id": "S1", "kind": "SESSION_END"})
    ev = q.get_nowait()
    assert ev["type"] == "result_ready" and ev["id"] is None
    assert set(ev["alert"]) == {"session_id", "status", "ended_at"} and ev["alert"]["session_id"] == "S1" and ev["alert"]["status"].startswith("ENDED")
    app.orchestrator.handle_turn(turn_id="e1", frontend_message={"session_id": "S1", "kind": "SESSION_END"})      # 재전송: 다시 알리지 않는다
    assert q.empty()


def test_result_ready_wire_format_has_no_id_and_no_subject():
    raw = _sse({"type": "result_ready", "id": None, "alert": {"session_id": "S1", "status": "ENDED_COMPLETE", "ended_at": "2026-11-03T02:00:00Z"}}).decode()
    assert raw.startswith("event: result_ready\ndata: ") and "id:" not in raw and "subject" not in raw


def test_exposure_opens_results_to_clinician_accounts_and_not_to_others():
    routes = json.loads((ROOT / "deploy-exposure.json").read_text(encoding="utf-8"))["routes"]
    by_path = {p: r for r in routes for p in r["paths"]}
    for path, method in (("/v1/results", "GET"), ("/v1/results/*", "GET"), ("/v1/results/*/ack", "POST")):
        r = by_path[path]
        assert r["methods"] == [method] and r["auth"] == {"kind": "account", "roles": ["clinician"]} and r["upstream_token"] == "survey-result-reader"
    assert by_path["/v1/sessions/*/risk-signals"]["auth"]["kind"] == "denied" and by_path["/metrics"]["auth"]["kind"] == "denied"


def test_session_result_example_with_ack_fields_is_valid_and_bad_ack_is_rejected():
    ex = json.loads((ROOT / "schema/schemas/examples/SessionResult.valid.json").read_text(encoding="utf-8"))
    assert ex["acknowledged_by"] and RESULT_V.is_valid(ex)
    assert not RESULT_V.is_valid({**ex, "acknowledged_at": "yesterday"}) and not RESULT_V.is_valid({**ex, "acknowledged_by": ""})
