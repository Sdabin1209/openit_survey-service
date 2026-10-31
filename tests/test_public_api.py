"""공개 API(PUBLIC_API.md): 검증, 인증, 멱등, 상태 코드, 유휴 만료, HTTP 경계 뒤의 추론."""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import jsonschema
import pytest

import survey_schema
from survey_service.components.link_public_api import PublicApi
from survey_service.wiring.orchestrator import Orchestrator
from survey_service.components.release_registry import ReleaseRegistry
from survey_service.wiring.app import App
from survey_service.server import serve
from survey_service.settings import Settings
from survey_service.components.session_store import MemorySessionStore

from .timing import HANG_GUARD_SECONDS, wait_until
from .http_server_support import start_server, server_port, stop_server
from .e2e_support import ANSWER_OUT, RISK_OUT, Rig
from .fakes import FakeReleaseReader, FakeTraceIngest, RecordingConsole, RecordingDispatcher
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED

AUTH = "Bearer tok"
FI = jsonschema.Draft202012Validator(survey_schema.load_schema("FrontendInput"))


def call(api, method, path, body=None, auth=AUTH, raw=None):
    data = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode() if body is not None else b"")
    return api.handle(method, path, auth, data, user="staff-1")


@pytest.fixture
def rig_api():
    rig = Rig(risk=True)
    return rig, PublicApi(rig.orch, "tok", max_utterance_chars=100)


def start(api, sid="K1"):
    return call(api, "POST", "/v1/sessions", {"session_id": sid, "kind": "SESSION_START"})


def say(api, text, tid, sid="K1"):
    return call(api, "POST", f"/v1/sessions/{sid}/turns", {"session_id": sid, "kind": "UTTERANCE", "utterance": text, "turn_id": tid})


def test_start_turn_end_happy_path(rig_api):
    rig, api = rig_api
    code, body, _ = start(api)
    assert code == 200 and body["response_text"] and body["turn_failure_signal"] is None and not list(FI.iter_errors(body))
    rig.model.queue.append(ANSWER_OUT)
    code, body, _ = say(api, "안 피움", "t-1")
    assert code == 200 and body["response_text"] and not list(FI.iter_errors(body))
    assert rig.doc("K1")["answer_records"][0]["current_value"] == {"answer": False}
    code, body, _ = call(api, "POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END"})
    assert code == 200 and not list(FI.iter_errors(body))
    assert rig.doc("K1")["progress_record"]["session_status"] == "ENDED_LEFT" and len(rig.dispatcher.inputs) == 1


def test_same_turn_id_returns_same_response_and_processes_once(rig_api):
    rig, api = rig_api
    start(api)
    rig.model.queue.append(ANSWER_OUT)
    a = say(api, "안 피움", "t-1")
    n = len(rig.doc("K1")["dialogue_log"])
    rig.model.queue.append(ANSWER_OUT)                    # 두 번째 처리가 있었다면 소비됐을 것
    b = say(api, "안 피움", "t-1")
    assert a[:2] == b[:2] and len(rig.doc("K1")["dialogue_log"]) == n and len(rig.model.queue) == 1
    assert start(api)[:2] == start(api)[:2]               # 시작 재시도도 같은 응답
    code, body, _ = say(api, "다른 말", "t-1")            # 같은 turn_id, 다른 입력
    assert code == 409 and body["code"] == "CONFLICT"


def test_auth_and_routes(rig_api):
    rig, api = rig_api
    for auth in ("", "Bearer", "Bearer wrong", "Basic tok"):
        code, body, hdr = call(api, "POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START"}, auth=auth)
        assert code == 401 and body["code"] == "PERMISSION_DENIED" and hdr["WWW-Authenticate"] == "Bearer"
    assert rig.store.load("K1")["status"] == "NOT_FOUND"
    assert call(api, "POST", "/v1/nope", {})[0] == 404 and call(api, "GET", "/v1/sessions", None)[0] == 405      # 알려진 경로의 다른 메서드는 405(openit_link 규약, 옛 동작은 404)
    assert call(api, "POST", "/v1/sessions/K1/turns/x", {})[0] == 404


@pytest.mark.parametrize("path,body", [
    ("/v1/sessions", {"session_id": "K1", "kind": "UTTERANCE"}),
    ("/v1/sessions", {"session_id": "K1", "kind": "SESSION_START", "x": 1}),
    ("/v1/sessions", {"session_id": "a" * 65, "kind": "SESSION_START"}),
    ("/v1/sessions", {"session_id": "has.dot", "kind": "SESSION_START"}),
    ("/v1/sessions", {"session_id": "", "kind": "SESSION_START"}),
    # session_id 없는 시작 요청은 2.2.0부터 유효하다(서버 발급): tests/test_server_issued_session_id.py
    ("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "x"}),                       # turn_id 없음
    ("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "x", "turn_id": "bad id"}),
    ("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "x", "turn_id": "start"}),     # 예약 값
    ("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "  ", "turn_id": "t"}),
    ("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "가" * 101, "turn_id": "t"}),
    ("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": 5, "turn_id": "t"}),
    ("/v1/sessions/K2/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "x", "turn_id": "t"}),        # 경로와 본문 불일치
    ("/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_START"}),
])
def test_bad_requests_are_400_and_change_nothing(rig_api, path, body):
    rig, api = rig_api
    start(api)
    snap = rig.doc("K1")
    code, rec, _ = call(api, "POST", path, body)
    assert code == 400 and rec["code"] == "INVALID_RECORD"
    assert rig.doc("K1") == snap


def test_malformed_bodies(rig_api):
    rig, api = rig_api
    assert call(api, "POST", "/v1/sessions", raw=b"{not json")[0] == 400
    assert call(api, "POST", "/v1/sessions", raw=b"[1]")[0] == 400
    assert call(api, "POST", "/v1/sessions", raw=b"")[0] == 400
    assert call(api, "POST", "/v1/sessions", raw=b"x" * 70000)[0] == 413      # 본문 상한 초과는 413(openit_link 규약, 옛 동작은 400)
    assert call(api, "POST", "/v1/sessions", raw=b"\xff\xfe")[0] == 400


def test_unknown_and_ended_sessions(rig_api):
    rig, api = rig_api
    assert say(api, "안 피움", "t-1", sid="NOPE")[0] == 404 and rig.store.load("NOPE")["status"] == "NOT_FOUND"
    assert call(api, "POST", "/v1/sessions/NOPE/end", {"session_id": "NOPE", "kind": "SESSION_END"})[0] == 404
    first = start(api)
    end = call(api, "POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END"})
    assert end[0] == 200
    code, body, _ = say(api, "안 피움", "t-2")
    assert code == 409 and body["code"] == "CONFLICT"
    assert start(api)[:2] == first[:2]                    # 시작 재시도는 처음 응답 그대로(session_id당 한 번만 시작한다)
    again = call(api, "POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END"})
    assert again[:2] == end[:2] and len(rig.dispatcher.inputs) == 1           # 끝난 세션의 종료 재시도는 같은 결과


def test_paused_session_gets_nothing_to_say_but_200(rig_api):
    rig, api = rig_api
    start(api)
    rig.model.queue.append(RISK_OUT)
    code, body, _ = say(api, "숨이 안 쉬어져요", "t-1")
    assert code == 200 and body["response_text"] and rig.console.inputs
    assert say(api, "언제 와요", "t-2")[:2] == (200, {"response_text": None, "turn_failure_signal": None})


def test_failure_signal_is_passed_through_with_200(rig_api):
    rig, api = rig_api
    start(api)
    rig.console.fail = True
    rig.model.queue.append(RISK_OUT)
    code, body, _ = say(api, "숨이 안 쉬어져요", "t-1")
    assert code == 200 and body["turn_failure_signal"] == {"session_id": "K1", "write_committed": "COMMITTED", "staff_alert": "DELIVERY_FAILED", "pause_reason": "RISK"}
    assert body["response_text"]                        # 응답 단계는 그대로 한다


def test_no_active_release_is_503():
    from .fakes import FaultyStore
    api = PublicApi(Orchestrator(MemorySessionStore(), ReleaseRegistry(), staff_console=RecordingConsole()), "tok")
    code, body, _ = start(api)
    assert code == 503 and body["code"] == "UNAVAILABLE" and body["retryable"] is True
    rig = Rig()
    rig.orch.store = FaultyStore(rig.store, fail_load=True)
    code, body, _ = start(PublicApi(rig.orch, "tok"))
    assert code == 503
    assert say(PublicApi(rig.orch, "tok"), "x", "t")[0] == 503


def test_too_many_in_flight_is_429():
    rig = Rig()
    api = PublicApi(rig.orch, "tok", max_inflight=1)
    start(api)
    gate, entered = threading.Event(), threading.Event()
    real = rig.release.session.extract

    def slow(inp, **kw):
        entered.set(); gate.wait(HANG_GUARD_SECONDS)
        return real(inp, **kw)
    rig.release.session.extract = slow
    t = threading.Thread(target=lambda: say(api, "느린 말", "t-slow"))
    t.start()
    assert entered.wait(HANG_GUARD_SECONDS)
    code, body, hdr = say(api, "다른 말", "t-2", sid="K1")
    assert code == 429 and hdr["Retry-After"] == "1"
    gate.set(); t.join()
    assert say(api, "또 다른 말", "t-3")[0] == 200            # 비면 다시 받는다


# ---------------------------------------------------------------- 유휴 만료
def test_idle_sessions_are_closed_as_left_but_paused_are_not():
    now = [1000.0]
    rig = Rig(risk=True, store=MemorySessionStore(clock=lambda: now[0]))
    api = PublicApi(rig.orch, "tok")
    start(api, "OLD"); start(api, "PAUSED")
    rig.model.queue.append(RISK_OUT)
    say(api, "숨이 안 쉬어져요", "t-r", sid="PAUSED")
    now[0] += 200
    start(api, "FRESH")
    now[0] += 150                                          # OLD: 350초, PAUSED: 350초, FRESH: 150초
    idle = rig.store.list_idle(now[0] - 300)
    assert idle == ["OLD"]                                 # 멈춘 세션은 직원이 정한다
    assert api.expire_idle(idle) == 1
    assert rig.doc("OLD")["progress_record"]["session_status"] == "ENDED_LEFT" and len(rig.dispatcher.inputs) == 1
    assert rig.doc("FRESH")["progress_record"]["session_status"] == "IN_PROGRESS"
    assert api.expire_idle(idle) == 0                      # 다시 해도 한 번만 닫힌다
    code, body, _ = say(api, "늦은 말", "t-late", sid="OLD")
    assert code == 409
    assert rig.store.list_idle(now[0] - 300) == []


def test_activity_resets_idle_clock():
    now = [0.0]
    rig = Rig(risk=True, store=MemorySessionStore(clock=lambda: now[0]))
    api = PublicApi(rig.orch, "tok")
    start(api)
    now[0] = 290
    say(api, "말", "t-1")
    now[0] = 400
    assert rig.store.list_idle(now[0] - 300) == []


def test_sqlite_list_idle_and_migration(tmp_path):
    import sqlite3
    from survey_service.components import session_store as effects
    from survey_service.components.session_store import SqliteSessionStore
    from .sample_bank import sample_bank
    p = str(tmp_path / "old.db")
    db = sqlite3.connect(p)                                 # updated_at 열이 없는 이전 파일
    db.executescript("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, release_id TEXT NOT NULL, status TEXT NOT NULL, doc TEXT NOT NULL);")
    db.execute("INSERT INTO sessions VALUES ('A',1,'r','IN_PROGRESS','{}')")
    db.commit(); db.close()
    now = [100.0]
    s = SqliteSessionStore(p, clock=lambda: now[0])
    s.create("B", "r", effects.initial_state("B", sample_bank(), "Q1.D01"))
    assert s.list_idle(50) == ["A"] and s.list_idle(200) == ["A", "B"]      # 이전 세션은 updated_at 0으로 보고 가장 먼저 만료
    s.close()


# ---------------------------------------------------------------- HTTP로 본 공개 API
def test_over_http_with_app(tmp_path):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", sample_bank(), action_rules_version=INSTALLED["action_rules_version"],
                       model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    from .fakes import FakeTraceIngest
    ing = FakeTraceIngest()
    s = Settings(session_db=str(tmp_path / "s.db"), outbox_db=str(tmp_path / "o.db"), work_dir=str(tmp_path / "w"),
                 release_reader="x:y", trace_ingest="x:y", model_backend="fake", export_interval_seconds=0.05, session_idle_seconds=0)
    app = App(s, reader=reader, ingest=ing, staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
    app.start()
    httpd = serve(app, 0)
    port = server_port(httpd)
    start_server(httpd)

    def http(method, path, body=None, token="test-api-token-0123"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=HANG_GUARD_SECONDS) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        assert http("POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START"}, token=None)[0] == 401
        code, fi = http("POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START"})
        assert code == 200 and fi["response_text"] and fi["turn_failure_signal"] is None
        turn = {"session_id": "K1", "kind": "UTTERANCE", "utterance": "안녕하세요", "turn_id": "t-1"}
        c1, b1 = http("POST", "/v1/sessions/K1/turns", turn)
        c2, b2 = http("POST", "/v1/sessions/K1/turns", turn)
        assert c1 == c2 == 200 and b1 == b2
        assert http("POST", "/v1/sessions/ZZ/turns", dict(turn, session_id="ZZ"))[0] == 404
        assert http("POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END"})[0] == 200
        assert http("GET", "/healthz")[0] == 200 and http("GET", "/readyz", token=None)[0] == 200
        wait_until(lambda: len(ing.received) >= 3)
        assert {k[1] for k in ing.received} == {"start", "t-1", "end"}          # 턴마다 Trace 한 건(재시도 응답은 다시 보내지 않는다)
    finally:
        stop_server(httpd); app.stop()


def test_app_starts_without_kiosk_token_because_device_auth_is_the_gateway_header(tmp_path, monkeypatch):
    """ADR-0031: 기기 인증은 gateway mTLS가 넘기는 X-Openit-Device다. 키오스크 토큰은 2.x 호환이라 없어도 시작하고, 직원 토큰은 여전히 필요하다."""
    monkeypatch.delenv("SURVEY_API_TOKEN")
    s = Settings(session_db=str(tmp_path / "s.db"), outbox_db=str(tmp_path / "o.db"), work_dir=str(tmp_path / "w"),
                 release_reader="x:y", trace_ingest="x:y", model_backend="fake")
    app = App(s, reader=FakeReleaseReader(tmp_path / "gate"), ingest=FakeTraceIngest())
    try:
        assert app.api.token is None and app.api.handle("POST", "/v1/sessions", "", b"{}")[0] == 401
    finally:
        app.stop()


# ---------------------------------------------------------------- 직원 개입 (PUBLIC_API.md 직원 콘솔)
STAFF = "Bearer stok"


@pytest.fixture
def paused():
    rig = Rig(risk=True)
    api = PublicApi(rig.orch, "tok", staff_token="stok")
    start(api)
    rig.model.queue.append(RISK_OUT)
    say(api, "숨이 안 쉬어져요", "t-risk")
    assert rig.doc("K1")["progress_record"]["session_status"] == "PAUSED_RISK"
    return rig, api


def intervene(api, decision, tid, sid="K1", auth=STAFF, **extra):
    return call(api, "POST", f"/v1/sessions/{sid}/intervention", {"session_id": sid, "decision": decision, "turn_id": tid, **extra}, auth=auth)


def test_staff_resumes_paused_session_and_conversation_continues(paused):
    rig, api = paused
    code, body, _ = intervene(api, "RESUME", "i-1")
    assert code == 200 and body["response_text"] and body["turn_failure_signal"] is None and not list(FI.iter_errors(body))
    assert rig.doc("K1")["progress_record"]["session_status"] == "IN_PROGRESS"
    assert rig.console.inputs[-1]["intervention_result"] == {"session_id": "K1", "result": "APPLIED"}
    rig.model.queue.append(ANSWER_OUT)
    assert say(api, "안 피움", "t-after")[0] == 200
    assert rig.doc("K1")["answer_records"][0]["current_value"] == {"answer": False}


def test_staff_ends_paused_session(paused):
    rig, api = paused
    code, body, _ = intervene(api, "END", "i-1")
    assert code == 200 and rig.doc("K1")["progress_record"]["session_status"] == "ENDED_STAFF" and len(rig.dispatcher.inputs) == 1
    assert say(api, "x", "t-x")[0] == 409


def test_intervention_is_idempotent_and_conflicts_on_different_input(paused):
    rig, api = paused
    a = intervene(api, "RESUME", "i-1")
    n = len(rig.doc("K1")["dialogue_log"])
    b = intervene(api, "RESUME", "i-1")                      # 이미 풀렸지만 같은 turn_id는 같은 응답
    assert a[:2] == b[:2] and len(rig.doc("K1")["dialogue_log"]) == n
    code, body, _ = intervene(api, "END", "i-1")
    assert code == 409 and body["code"] == "CONFLICT"


def test_roles_are_separated(paused):
    rig, api = paused
    assert intervene(api, "RESUME", "i-1", auth=AUTH)[0] == 403                # 키오스크 토큰
    assert intervene(api, "RESUME", "i-1", auth="Bearer nope")[0] == 401
    assert intervene(api, "RESUME", "i-1", auth="")[0] == 401
    assert rig.doc("K1")["progress_record"]["session_status"] == "PAUSED_RISK"
    for method, path, body in [("POST", "/v1/sessions", {"session_id": "K9", "kind": "SESSION_START"}),
                               ("POST", "/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "x", "turn_id": "t"}),
                               ("POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END"})]:
        code, rec, _ = call(api, method, path, body, auth=STAFF)                # 직원 토큰으로 키오스크 경로
        assert code == 403 and rec["code"] == "PERMISSION_DENIED"
    assert rig.store.load("K9")["status"] == "NOT_FOUND"


def test_intervention_state_checks(rig_api):
    rig, _ = rig_api
    api = PublicApi(rig.orch, "tok", staff_token="stok")
    assert intervene(api, "RESUME", "i-1")[0] == 404                           # 모르는 세션
    start(api)
    code, body, _ = intervene(api, "RESUME", "i-2")                            # 멈추지 않은 세션
    assert code == 409 and body["code"] == "CONFLICT" and rig.doc("K1")["progress_record"]["session_status"] == "IN_PROGRESS"
    assert intervene(api, "END", "i-3")[0] == 409
    call(api, "POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END"})
    assert intervene(api, "RESUME", "i-4")[0] == 409                           # 끝난 세션


@pytest.mark.parametrize("body", [
    {"session_id": "K1", "decision": "PAUSE", "turn_id": "i"}, {"session_id": "K1", "turn_id": "i"},
    {"session_id": "K1", "decision": "RESUME"}, {"session_id": "K1", "decision": "RESUME", "turn_id": "end"},
    {"session_id": "K1", "decision": "RESUME", "turn_id": "bad id"}, {"session_id": "K2", "decision": "RESUME", "turn_id": "i"},
    {"session_id": "K1", "decision": "RESUME", "turn_id": "i", "x": 1}, {"decision": "RESUME", "turn_id": "i"},
])
def test_intervention_bad_requests(paused, body):
    rig, api = paused
    snap = rig.doc("K1")
    code, rec, _ = call(api, "POST", "/v1/sessions/K1/intervention", body, auth=STAFF)
    assert code == 400 and rec["code"] == "INVALID_RECORD" and rig.doc("K1") == snap


def test_tokens_must_differ():
    rig = Rig()
    with pytest.raises(ValueError):
        PublicApi(rig.orch, "same", staff_token="same")
    with pytest.raises(ValueError):
        PublicApi(rig.orch, "tok", staff_token="")


def test_app_requires_distinct_staff_token(tmp_path, monkeypatch):
    from survey_service.settings import ConfigError
    s = Settings(session_db=str(tmp_path / "s.db"), outbox_db=str(tmp_path / "o.db"), work_dir=str(tmp_path / "w"),
                 release_reader="x:y", trace_ingest="x:y", model_backend="fake")
    monkeypatch.delenv("SURVEY_STAFF_API_TOKEN")
    with pytest.raises(ConfigError):
        App(s, reader=object(), ingest=object())
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "test-api-token-0123")
    with pytest.raises(ConfigError):
        App(s, reader=object(), ingest=object())
