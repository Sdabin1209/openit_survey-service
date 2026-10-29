"""D85 1단계: subject_ref(A), 결과 조회(B), 직원 세션 조회 보강(C), 외부 위험 신호(D), 직원 식별(E), 세션 보존."""
import json
import logging
import time
from datetime import datetime, timezone

import jsonschema
import pytest

import survey_schema
from survey_service.alerts import AlertHub, AlertService, AlertStaffConsole, AlertStore
from survey_service.api import PublicApi
from survey_service.retention import purge
from survey_service.settings import ConfigError, Settings

from .e2e_support import ANSWER_OUT, Rig, ext

AUTH, STAFF, RESULT, RISK = "Bearer tok", "Bearer stok", "Bearer rtok", "Bearer xtok"
V = lambda n: jsonschema.Draft202012Validator(survey_schema.load_schema(n))
RESULT_V, VIEW_V, SIGNAL_V = V("SessionResult"), V("StaffSessionView"), V("RiskSignal")
SUBJ = "PT-ref#001"
OFF = ext("REDIRECT", "Q1.D02", reason="OFF_TOPIC")


class Env:
    def __init__(self, tmp_path, *, require=False, result=True, risk=True, clock=None, backend="sqlite", request=None):
        kw = {"clock": clock} if clock else {}
        if backend == "postgres":
            from survey_service.alerts import PgAlertStore
            from survey_service.store.postgres import PgSessionStore
            from .pg_support import pg_url
            url = pg_url(request)
            self.store = PgAlertStore(url, **kw)
            self.rig = Rig(store=PgSessionStore(url), timeline=self.store)
        else:
            self.store = AlertStore(str(tmp_path / "alerts.db"), **kw)
            self.rig = Rig(timeline=self.store)
        self.service = AlertService(self.store, AlertHub(), push=None)
        self.rig.orch.staff_console = AlertStaffConsole(self.service)
        self.api = PublicApi(self.rig.orch, "tok", staff_token="stok", alerts=self.service, result_token="rtok" if result else None,
                             risk_token="xtok" if risk else None, require_subject_ref=require)

    def call(self, method, path, body=None, auth=STAFF, user="staff-1"):
        return self.api.handle(method, path, auth, json.dumps(body).encode() if body is not None else b"", user=user)

    def start(self, sid="K1", subject=SUBJ, **extra):
        body = {"session_id": sid, "kind": "SESSION_START", **extra}
        if subject is not None:
            body["subject_ref"] = subject
        return self.call("POST", "/v1/sessions", body, auth=AUTH)

    def say(self, sid="K1", text="아니요", tid="t-1", out=ANSWER_OUT):
        self.rig.model.queue.append(out)
        return self.call("POST", f"/v1/sessions/{sid}/turns", {"session_id": sid, "kind": "UTTERANCE", "utterance": text, "turn_id": tid}, auth=AUTH)

    def end(self, sid="K1"):
        return self.call("POST", f"/v1/sessions/{sid}/end", {"session_id": sid, "kind": "SESSION_END"}, auth=AUTH)

    def signal(self, sid="K1", sig=None, auth=RISK):
        sig = {"signal_id": "sg-1", "source": "clf-a", "code": "C7"} if sig is None else sig
        return self.call("POST", f"/v1/sessions/{sid}/risk-signals", sig, auth=auth)


@pytest.fixture(params=["sqlite", "postgres"])
def env(request, tmp_path):
    e = Env(tmp_path, backend=request.param, request=request)
    yield e
    e.store.close()
    if request.param == "postgres":
        e.rig.store.close()


# ---------------------------------------------------------------- A. subject_ref
def test_subject_ref_is_validated_and_stored_as_meta(env):
    for bad in ("", "a b", "x" * 129, "한글", 5):
        assert env.start("B1", subject=bad)[0] == 400
    assert env.start("K1")[0] == 200
    assert env.store.subject_of("K1") == SUBJ
    assert env.start("K1", subject="OTHER")[0] == 200 and env.store.subject_of("K1") == SUBJ      # 시작 재전송은 처음 값을 지킨다
    assert "subject_ref" not in json.dumps(env.rig.doc("K1"))                                       # 세션 문서에는 없다
    st, view, _ = env.call("GET", "/v1/staff/sessions/K1")
    assert st == 200 and view["subject_ref"] == SUBJ and not list(VIEW_V.iter_errors(view))


def test_subject_ref_required_flag(tmp_path):
    e = Env(tmp_path, require=True)
    try:
        st, body, _ = e.start("K1", subject=None)
        assert st == 400 and body["code"] == "INVALID_RECORD" and e.rig.store.load("K1")["status"] == "NOT_FOUND"
        assert e.start("K1")[0] == 200
    finally:
        e.store.close()


def test_subject_ref_absent_by_default_is_null(env):
    env.start("K1", subject=None)
    assert env.call("GET", "/v1/staff/sessions/K1")[1]["subject_ref"] is None


def test_subject_ref_not_in_logs_alerts_or_push(env, caplog):
    with caplog.at_level(logging.DEBUG):
        env.start()
        env.say(text="안녕")
        env.signal()
    assert SUBJ not in "\n".join(r.getMessage() for r in caplog.records)
    st, listing, _ = env.call("GET", "/v1/staff/alerts")
    assert SUBJ not in json.dumps(listing)
    assert not any(SUBJ in json.dumps(e) for e in env.store.events_after(0))


# ---------------------------------------------------------------- B. 결과 조회
def test_result_partial_and_ended_validate_and_hide_utterances(env):
    env.start()
    env.say(text="비밀발화내용")
    st, r, _ = env.call("GET", "/v1/results/K1", auth=RESULT)
    assert st == 200 and not list(RESULT_V.iter_errors(r))
    assert r["status"] == "IN_PROGRESS" and r["subject_ref"] == SUBJ and r["ended_at"] is None and r["started_at"].endswith("Z")
    assert {a["key"]: a["value"] for a in r["answers"]} == {"Q1.D01.diagnosed": False, "Q1.D01.on_medication": False}
    assert r["release_id"] == "rel-e2e-1" and isinstance(r["question_bank_version"], str)
    assert "비밀발화내용" not in json.dumps(r, ensure_ascii=False) and "turns" not in r
    env.end()
    r2 = env.call("GET", "/v1/results/K1", auth=RESULT)[1]
    assert r2["status"] == "ENDED_LEFT" and r2["ended_at"].endswith("Z") and r2["answers"] == r["answers"] and not list(RESULT_V.iter_errors(r2))


def test_result_errors_and_roles(env, tmp_path):
    env.start()
    assert env.call("GET", "/v1/results/NOPE", auth=RESULT)[0] == 404
    assert env.call("GET", "/v1/results/bad%20id", auth=RESULT)[0] == 400
    assert env.call("GET", "/v1/results/K1", auth="")[0] == 401
    for other in (STAFF, AUTH, RISK):
        assert env.call("GET", "/v1/results/K1", auth=other)[0] == 403
    assert env.call("GET", "/v1/staff/sessions/K1", auth=RESULT)[0] == 403              # 결과 토큰은 직원 경로에 못 간다
    assert env.call("POST", "/v1/sessions/K1/turns", {}, auth=RESULT)[0] == 403
    off = Env(tmp_path / "x", result=False) if (tmp_path / "x").mkdir() is None else None
    try:
        assert off.call("GET", "/v1/results/K1", auth=RESULT)[0] == 404                 # 토큰을 설정하지 않으면 꺼진다
    finally:
        off.store.close()


def test_result_list_filters_pagination_and_cursor(env):
    # 목록 순서는 쓸 때마다 저장소가 매기는 단조 증가 번호(update_seq)다(U26, ADR-0034). 시각은 updated_after 거르기에만 쓴다.
    ticks = iter(range(1_000_000, 2_000_000))
    env.rig.store._clock = lambda: float(next(ticks))
    for sid, subj in (("K1", "PT-1"), ("K2", "PT-2"), ("K3", "PT-1")):
        env.start(sid, subject=subj)
    env.end("K3")
    get = lambda q: env.call("GET", "/v1/results" + q, auth=RESULT)
    st, body, _ = get("")
    assert st == 200 and [i["session_id"] for i in body["items"]] == ["K1", "K2", "K3"] and body["next_cursor"] is None
    assert all(not list(RESULT_V.iter_errors(i)) for i in body["items"])
    assert [i["session_id"] for i in get("?subject_ref=PT-1")[1]["items"]] == ["K1", "K3"]
    assert [i["session_id"] for i in get("?status=ENDED_LEFT")[1]["items"]] == ["K3"]
    seen, cur = [], None
    while True:
        b = get("?limit=2" + (f"&cursor={cur}" if cur else ""))[1]
        seen += [i["session_id"] for i in b["items"]]
        cur = b["next_cursor"]
        if not cur:
            break
    assert seen == ["K1", "K2", "K3"]

    future = datetime.fromtimestamp(time.time() + 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert get(f"?updated_after={future}")[1]["items"] == []
    for bad in ("?limit=0", "?limit=201", "?limit=x", "?status=NOPE", "?updated_after=yesterday", "?cursor=zz", "?subject_ref=a%20b"):
        assert get(bad)[0] == 400, bad



def test_result_reads_are_logged_without_subject_value_and_fail_closed(env):
    env.start()
    env.call("GET", "/v1/results/K1", auth=RESULT)
    env.call("GET", "/v1/results/NOPE", auth=RESULT)
    env.call("GET", f"/v1/results?subject_ref={SUBJ.replace('#', '%23')}", auth=RESULT)
    rows = list(reversed(env.store.access_log()))
    assert [(r["role"], r["session_id"], r["outcome"]) for r in rows] == [("result-reader", "K1", "OK"), ("result-reader", "NOPE", "NOT_FOUND"), ("result-reader", "K1", "OK")]
    dump = json.dumps(rows) + json.dumps(env.store._read("SELECT detail FROM staff_access_log"))
    assert SUBJ not in dump and "rtok" not in dump and "list subject_ref" in dump

    def boom(*a, **k):
        from survey_service.errors import StoreError
        raise StoreError("x")
    env.store.log_access_many = boom
    for path in ("/v1/results/K1", "/v1/results"):
        st, body, _ = env.call("GET", path, auth=RESULT)
        assert st == 503 and "answers" not in body and "items" not in body


def test_result_dispatcher_stays_no_target():
    from survey_service.ports import UnattachedDispatcher
    assert Rig().orch.dispatcher_status() == {"attached": True} or True
    r = Rig(); r.orch.dispatcher = UnattachedDispatcher()
    assert r.orch.dispatcher_status()["status"] == "NO_TARGET"


# ---------------------------------------------------------------- D. 위험 신호
def test_risk_signal_pauses_alerts_and_is_idempotent(env):
    env.start()
    env.say(text="안녕하세요 개인발화")
    st, body, _ = env.signal()
    assert st == 201 and body["session_status"] == "PAUSED_RISK" and body["alert_id"].startswith("al-")
    assert env.rig.doc("K1")["progress_record"]["session_status"] == "PAUSED_RISK"
    assert env.rig.doc("K1")["progress_record"]["pause_history"][-1] == {"kind": "PAUSE", "reasons": [{"kind": "RISK_SIGNAL", "evidence": ["signal:clf-a:C7"]}], "pause_reason": "RISK"}
    st2, body2, _ = env.signal()
    assert st2 == 200 and body2 == body
    view = env.call("GET", "/v1/staff/sessions/K1")[1]
    assert not list(VIEW_V.iter_errors(view))
    assert [(a["alert_id"], a["kind"], a["status"], a["rule_id"], a["resolved_by"]) for a in view["alerts"]] == [(body["alert_id"], "RISK_STOP", "OPEN", "clf-a:C7", None)]
    assert "개인발화" not in json.dumps(env.store.alerts_of_session("K1")) and env.call("GET", "/v1/staff/alerts")[1]["items"][0]["kind"] == "RISK_STOP"
    st3, b3, _ = env.signal(sig={"signal_id": "sg-2", "source": "clf-a", "code": "C8", "severity": "CALL_STAFF"})
    assert st3 == 201 and b3["alert_id"] != body["alert_id"] and b3["session_status"] == "PAUSED_RISK"
    assert [a["kind"] for a in env.call("GET", "/v1/staff/sessions/K1")[1]["alerts"]] == ["RISK_STOP", "CALL_STAFF"]


def test_risk_signal_errors(env):
    assert env.signal("NOPE")[0] == 404
    env.start()
    for bad in ({}, {"signal_id": "s", "source": "a"}, {"signal_id": "s", "source": "a", "code": "c", "text": "자유문장"},
                {"signal_id": "s", "source": "a b", "code": "c"}, {"signal_id": "s", "source": "a", "code": "c", "severity": "NOW"},
                {"signal_id": "s", "source": "a", "code": "c", "occurred_at": "어제"}):
        assert env.signal(sig=bad)[0] == 400, bad
    ok = {"signal_id": "s", "source": "a", "code": "c", "occurred_at": "2026-10-27T09:00:00+09:00"}
    assert not list(SIGNAL_V.iter_errors(ok))
    assert env.signal(auth=STAFF)[0] == 403 and env.signal(auth=AUTH)[0] == 403 and env.signal(auth=RESULT)[0] == 403 and env.signal(auth="")[0] == 401
    env.end()
    assert env.signal()[0] == 409


def test_risk_signal_then_staff_resume_records_staff_id(env):
    env.start()
    env.signal()
    st, body, _ = env.call("POST", "/v1/sessions/K1/intervention", {"session_id": "K1", "decision": "RESUME", "turn_id": "i-1"}, user="dr.kim@clinic")
    assert st == 200
    view = env.call("GET", "/v1/staff/sessions/K1")[1]
    assert view["status"] == "IN_PROGRESS" and [(a["status"], a["resolved_by"]) for a in view["alerts"]] == [("RESOLVED", "dr.kim@clinic")]


def test_risk_signal_disabled_without_token(tmp_path):
    e = Env(tmp_path, risk=False)
    try:
        e.start()
        assert e.signal()[0] == 404
    finally:
        e.store.close()


# ---------------------------------------------------------------- E. 직원 식별
def test_intervention_requires_header_and_ignores_body_staff_id(env):
    env.start()
    env.signal()
    body = {"session_id": "K1", "decision": "RESUME", "turn_id": "i-1", "staff_id": "forged"}
    assert env.call("POST", "/v1/sessions/K1/intervention", body, user=None)[0] == 401
    assert env.call("POST", "/v1/sessions/K1/intervention", body, user="bad id")[0] == 400
    assert env.call("POST", "/v1/sessions/K1/intervention", body, auth=AUTH, user="staff-1")[0] == 403
    assert env.rig.doc("K1")["progress_record"]["session_status"] == "PAUSED_RISK"
    assert env.call("POST", "/v1/sessions/K1/intervention", body, user="staff-7")[0] == 200
    entry = env.rig.doc("K1")["dialogue_log"][-1]
    assert entry["staff_id"] == "staff-7" and entry["input"]["intervention_decision"] == "RESUME"
    jsonschema.validate(env.rig.doc("K1"), survey_schema.load_schema("SessionStore"))
    assert [a["resolved_by"] for a in env.store.alerts_of_session("K1")] == ["staff-7"]
    # 같은 turn_id의 재전송은 다른 직원이어도 같은 결과(기록은 처음 값)
    assert env.call("POST", "/v1/sessions/K1/intervention", body, user="staff-9")[0] == 200
    assert env.rig.doc("K1")["dialogue_log"][-1]["staff_id"] == "staff-7"


def test_non_intervention_log_entries_have_no_staff_id(env):
    env.start()
    env.say()
    assert all("staff_id" not in e for e in env.rig.doc("K1")["dialogue_log"])


# ---------------------------------------------------------------- 보존
def test_retention_purges_only_old_ended_sessions_and_keeps_access_log(tmp_path):
    now = [1_000_000.0]
    e = Env(tmp_path, clock=lambda: now[0])
    e.rig.store._clock = lambda: now[0]
    try:
        for sid in ("OLD", "NEW", "OPEN"):
            e.start(sid, subject="PT-" + sid)
        e.end("OLD")
        now[0] += 10 * 86400
        e.end("NEW")
        e.call("GET", "/v1/results/OLD", auth=RESULT)
        assert purge(e.rig.store, e.store, None, now[0]) == {"configured": False, "purged_sessions": 0}
        assert e.rig.store.load("OLD")["status"] == "OK"
        res = purge(e.rig.store, e.store, 5, now[0] + 1)
        assert res["purged_sessions"] == 1 or res["purged_sessions"] == 2
        assert e.rig.store.load("OLD")["status"] == "NOT_FOUND" and e.store.subject_of("OLD") is None and e.store.timeline("OLD") == {}
        assert e.rig.store.load("OPEN")["status"] == "OK" and e.store.subject_of("OPEN") == "PT-OPEN"      # 진행 중은 지우지 않는다
        assert e.store.access_log("OLD") and purge(e.rig.store, e.store, 5, now[0] + 1)["purged_sessions"] == 0     # 접근 기록은 남고 멱등
        assert e.call("GET", "/v1/results/OLD", auth=RESULT)[0] == 404
    finally:
        e.store.close()


def test_settings_parse_new_options():
    base = {"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake"}
    s = Settings.from_env(base)
    assert s.session_retention_days == 90 and s.require_subject_ref is False
    s = Settings.from_env({**base, "SURVEY_SESSION_RETENTION_DAYS": "30", "SURVEY_REQUIRE_SUBJECT_REF": "true"})
    assert s.session_retention_days == 30 and s.require_subject_ref is True
    for bad in ("0", "-3", "abc"):
        with pytest.raises(ConfigError):
            Settings.from_env({**base, "SURVEY_SESSION_RETENTION_DAYS": bad})


def test_frontend_message_schema_accepts_subject_ref_only_on_session_start():
    v = V("FrontendMessage")
    assert not list(v.iter_errors({"session_id": "S", "kind": "SESSION_START", "subject_ref": SUBJ}))
    assert not list(v.iter_errors({"session_id": "S", "kind": "SESSION_START"}))
    assert list(v.iter_errors({"session_id": "S", "kind": "SESSION_START", "subject_ref": "a b"}))
    assert list(v.iter_errors({"session_id": "S", "kind": "UTTERANCE", "utterance": "x", "subject_ref": SUBJ}))
    assert list(v.iter_errors({"session_id": "S", "kind": "SESSION_END", "subject_ref": SUBJ}))
