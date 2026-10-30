"""직원 세션 조회(StaffSessionView)와 접근 기록, 위험 규칙 엔진, 위반 코드 관측, 타임아웃 분류, 결과 전달 대상 없음."""
import json
import logging
import sqlite3

import jsonschema
import pytest
from boundary_contracts import BoundaryError

import survey_schema
from survey_service.components.alert_service import AlertHub, AlertService, AlertStaffConsole
from survey_service.components.alert_book import AlertStore
from survey_service.components.link_public_api import PublicApi
from survey_service.components.link_inference import _timed_out
from survey_service.components.risk_screen import RuleRiskSource, parse_rules
from survey_service.wiring.app import App
from survey_service.settings import ConfigError, Settings

from .e2e_support import ANSWER_OUT, Rig, ext, val
from .fakes import FakeReleaseReader, FakeTraceIngest
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED

AUTH, STAFF = "Bearer tok", "Bearer stok"
TEST_PHRASE = "시험위험문구"
RULES = {"version": "t-1", "rules": [
    {"id": "t-phrase", "description": "시험용", "match": {"any_of": [{"contains": TEST_PHRASE}, {"regex": "시험정규식[0-9]+"}]}}]}
RISK_OUT = ext("REDIRECT", "Q1.D02", reason="OFF_TOPIC")       # converse() 뒤의 현재 문항은 Q1.D02
VIEW = jsonschema.Draft202012Validator(survey_schema.load_schema("StaffSessionView"))


class ViewRig:
    def __init__(self, tmp_path, rules=None, timeline=True):
        self.store = AlertStore(str(tmp_path / "alerts.db"))
        kw = {"timeline": self.store} if timeline else {}
        if rules is not None:
            kw["risk_source"] = RuleRiskSource(json.dumps(rules).encode())
        self.rig = Rig(**kw)
        self.service = AlertService(self.store, AlertHub(), push=None)
        self.rig.orch.staff_console = AlertStaffConsole(self.service)
        self.api = PublicApi(self.rig.orch, "tok", staff_token="stok", alerts=self.service)

    def call(self, method, path, body=None, auth=STAFF):
        return self.api.handle(method, path, auth, json.dumps(body).encode() if body is not None else b"")

    def post(self, path, body):
        return self.call("POST", path, body, auth=AUTH)


@pytest.fixture
def vr(tmp_path):
    r = ViewRig(tmp_path, rules=RULES)
    yield r
    r.store.close()


def converse(vr, sid="K1"):
    vr.post("/v1/sessions", {"session_id": sid, "kind": "SESSION_START", "turn_id": "start"})
    vr.rig.model.queue.append(ANSWER_OUT)
    vr.post(f"/v1/sessions/{sid}/turns", {"session_id": sid, "kind": "UTTERANCE", "utterance": "아니요 없어요", "turn_id": "t-1"})


# ---------------------------------------------------------------- StaffSessionView
def test_view_matches_schema_and_content(vr):
    converse(vr)
    st, body, _ = vr.call("GET", "/v1/staff/sessions/K1")
    assert st == 200 and not list(VIEW.iter_errors(body))
    assert set(body) == {"session_id", "subject_ref", "status", "release_id", "created_at", "turns", "values", "alerts"}
    assert body["session_id"] == "K1" and body["status"] == "IN_PROGRESS" and body["release_id"] == "rel-e2e-1"
    assert body["created_at"].endswith("Z")
    assert [t["turn_index"] for t in body["turns"]] == [0, 1] and [t["turn_id"] for t in body["turns"]] == ["start", "t-1"]
    assert body["turns"][1]["utterance"] == "아니요 없어요" and body["turns"][1]["action"] == "NEXT" and body["turns"][1]["at"]
    assert body["turns"][0]["utterance"] is None and body["turns"][0]["action"] is None
    assert {v["key"]: v["value"] for v in body["values"]} == {"Q1.D01.diagnosed": False, "Q1.D01.on_medication": False}
    assert all(v["status"] == "ANSWERED" and v["precision"] == "exact" for v in body["values"]) and body["alerts"] == []


def test_view_includes_alert_with_rule_id_and_no_raw_text_in_alert_surfaces(vr):
    converse(vr)
    vr.rig.model.queue.append(RISK_OUT)
    vr.post("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": f"저기 {TEST_PHRASE} 입니다", "turn_id": "t-2"})
    st, body, _ = vr.call("GET", "/v1/staff/sessions/K1")
    assert st == 200 and not list(VIEW.iter_errors(body)) and body["status"] == "PAUSED_RISK"
    assert [(a["kind"], a["status"], a["rule_id"]) for a in body["alerts"]] == [("RISK_STOP", "OPEN", "internal:t-phrase")]
    # 알림 저장소(알림·이벤트·접근 기록)와 알림 목록, 정지 기록에 발화 원문이 없다
    st, listing, _ = vr.call("GET", "/v1/staff/alerts")
    assert st == 200 and TEST_PHRASE not in json.dumps(listing, ensure_ascii=False)
    dump = json.dumps([vr.store._read(f"SELECT * FROM {t}") for t in ("alerts", "alert_events", "staff_access_log", "session_meta", "risk_signals",
                                                                      "session_timeline")], ensure_ascii=False)
    assert TEST_PHRASE not in dump and "저기" not in dump and "internal:t-phrase" in dump
    assert vr.rig.doc("K1")["progress_record"]["pause_history"][0]["reasons"] == [{"kind": "RISK_SIGNAL", "evidence": ["signal:internal:t-phrase"]}]


def test_view_not_found_and_auth(vr):
    converse(vr)
    st, body, _ = vr.call("GET", "/v1/staff/sessions/NOPE")
    assert st == 404 and body["code"] == "NOT_FOUND"
    assert vr.call("GET", "/v1/staff/sessions/K1", auth="")[0] == 401
    assert vr.call("GET", "/v1/staff/sessions/K1", auth="Bearer wrong")[0] == 401
    assert vr.call("GET", "/v1/staff/sessions/K1", auth=AUTH)[0] == 403          # 키오스크 토큰은 직원 경로에 못 간다
    assert vr.call("GET", "/v1/staff/sessions/bad%20id")[0] == 400
    assert vr.call("POST", "/v1/staff/sessions/K1", {})[0] == 405      # 알려진 경로의 다른 메서드는 405(옛 동작은 404)


def test_every_read_is_logged_without_secrets_or_utterances(vr):
    converse(vr)
    vr.call("GET", "/v1/staff/sessions/K1")
    vr.call("GET", "/v1/staff/sessions/K1")
    vr.call("GET", "/v1/staff/sessions/NOPE")
    vr.call("GET", "/v1/staff/sessions/K1", auth=AUTH)                           # 거부된 호출은 데이터를 내지 않으므로 이 기록에 없다
    rows = vr.store.access_log()
    assert [(r["role"], r["session_id"], r["outcome"]) for r in reversed(rows)] == [("staff", "K1", "OK"), ("staff", "K1", "OK"), ("staff", "NOPE", "NOT_FOUND")]
    assert all(r["at"].endswith("Z") for r in rows)
    text = json.dumps(rows, ensure_ascii=False)
    assert "stok" not in text and "아니요" not in text and "tok" not in text.replace("stok", "")
    assert set(rows[0]) == {"n", "at", "role", "session_id", "outcome", "user_id"}


def test_read_is_refused_when_access_cannot_be_logged(vr):
    converse(vr)

    def boom(*a, **k):
        from survey_service.components.session_store import StoreError
        raise StoreError("x")
    vr.store.log_access_many = boom
    st, body, _ = vr.call("GET", "/v1/staff/sessions/K1")
    assert st == 503 and "turns" not in body and "아니요" not in json.dumps(body, ensure_ascii=False)


def test_legacy_session_without_timeline_has_null_times_and_still_validates(tmp_path):
    r = ViewRig(tmp_path, timeline=False)
    try:
        converse(r)
        st, body, _ = r.call("GET", "/v1/staff/sessions/K1")
        assert st == 200 and not list(VIEW.iter_errors(body))
        assert body["created_at"] is None and all(t["at"] is None and t["turn_id"] is None for t in body["turns"])
    finally:
        r.store.close()


def test_view_is_read_only_and_does_not_change_session(vr):
    converse(vr)
    before = json.dumps(vr.rig.doc("K1"), sort_keys=True)
    vr.call("GET", "/v1/staff/sessions/K1")
    assert json.dumps(vr.rig.doc("K1"), sort_keys=True) == before


def test_alert_store_migrates_old_file_without_rule_id(tmp_path):
    p = str(tmp_path / "old.db")
    db = sqlite3.connect(p)
    db.executescript("CREATE TABLE alerts (n INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL,"
                     " status TEXT NOT NULL DEFAULT 'OPEN', resolved_by_decision TEXT, resolved_at TEXT);"
                     "INSERT INTO alerts(session_id, kind, created_at) VALUES ('S','RISK_STOP','2026-01-01T00:00:00Z');")
    db.commit(); db.close()
    s = AlertStore(p)
    assert s.alerts_of_session("S")[0]["rule_id"] is None
    s.close()


# ---------------------------------------------------------------- 규칙 엔진
@pytest.mark.parametrize("doc", [
    [], {"rules": []}, {"version": "", "rules": RULES["rules"]}, {"version": "v", "rules": []}, {"version": "v", "rules": RULES["rules"], "x": 1},
    {"version": "v", "rules": [{"id": "a b", "description": "d", "match": {"any_of": [{"contains": "x"}]}}]},
    {"version": "v", "rules": [{"id": "a", "description": "d", "match": {"any_of": []}}]},
    {"version": "v", "rules": [{"id": "a", "description": "d", "match": {"any_of": [{"regex": "("}]}}]},
    {"version": "v", "rules": [{"id": "a", "description": "d", "match": {"any_of": [{"contains": "x", "regex": "y"}]}}]},
    {"version": "v", "rules": [{"id": "a", "description": "d", "match": {"any_of": [{"contains": ""}]}}]},
    {"version": "v", "rules": [{"id": "a", "description": "d", "match": {"any_of": [{"like": "x"}]}}]},
    {"version": "v", "rules": [{"id": "a", "description": "d", "match": {"any_of": [{"contains": "x"}]}},
                               {"id": "a", "description": "d", "match": {"any_of": [{"contains": "y"}]}}]},
    {"version": "v", "rules": [{"id": "a", "match": {"any_of": [{"contains": "x"}]}}]},
])
def test_invalid_rule_files_are_rejected(doc):
    with pytest.raises(ConfigError):
        parse_rules(json.dumps(doc).encode())


def test_not_json_rejected():
    with pytest.raises(ConfigError):
        parse_rules(b"\xff\xfe")
    with pytest.raises(ConfigError):
        parse_rules(b"{not json")


def test_matching_contains_and_regex_first_rule_wins():
    src = RuleRiskSource(json.dumps({"version": "1", "rules": [
        {"id": "r1", "description": "d", "match": {"any_of": [{"contains": "ABC시험"}]}},
        {"id": "r2", "description": "d", "match": {"any_of": [{"regex": "시험[0-9]{2}"}, {"contains": "abc시험"}]}}]}).encode())
    assert src.match("그냥 말") is None
    assert src.match("xx abc시험 xx") == "r1"                       # 대소문자 무시, 파일 순서
    assert src.match("시험42") == "r2"
    alert = src.check(extract_input={}, extract_output={}, utterance="시험42입니다")
    assert alert == {"kind": "RISK", "reasons": [{"kind": "RISK_SIGNAL", "evidence": ["signal:internal:r2"]}]}
    assert src.check(extract_input={}, extract_output={}, utterance="평범") is None
    assert src.describe()["hits_by_rule"] == {"r2": 1}


def test_rule_hit_pauses_session_and_noop_otherwise(vr):
    converse(vr)
    vr.rig.model.queue.append(RISK_OUT)
    st, _, _ = vr.post("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "괜찮아요", "turn_id": "t-2"})
    assert st == 200 and vr.store.list(None, 10, None)[0] == []
    vr.rig.model.queue.append(RISK_OUT)
    st, body, _ = vr.post("/v1/sessions/K1/turns", {"session_id": "K1", "kind": "UTTERANCE", "utterance": "시험정규식77", "turn_id": "t-3"})
    assert st == 200      # 구독자가 없어 전달 판정은 DELIVERY_FAILED일 수 있다(알림은 저장된다)
    assert [a["rule_id"] for a in vr.store.alerts_of_session("K1")] == ["internal:t-phrase"]
    assert vr.rig.doc("K1")["progress_record"]["session_status"] == "PAUSED_RISK"


def _env(tmp_path, **extra):
    return {"SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"), "SURVEY_WORK_DIR": str(tmp_path / "w"),
            "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake", "SURVEY_SESSION_IDLE_SECONDS": "0", **extra}


def _app(tmp_path, **extra):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", sample_bank(), action_rules_version=INSTALLED["action_rules_version"],
                       model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    return App(Settings.from_env(_env(tmp_path, **extra)), reader=reader, ingest=FakeTraceIngest())


def test_readyz_reports_rules_file_and_start_log(tmp_path, caplog):
    f = tmp_path / "rules.json"
    raw = json.dumps(RULES).encode()
    f.write_bytes(raw)
    with caplog.at_level(logging.INFO, logger="survey_service"):
        app = _app(tmp_path, SURVEY_RISK_RULES_FILE=str(f))
    try:
        import hashlib
        src = app.readiness()[1]["staff_alert_source"]
        assert src["active"] is True and src["rule_count"] == 1 and src["version"] == "t-1" and src["sha256"] == hashlib.sha256(raw).hexdigest()
        assert any("위험 규칙 적재" in r.getMessage() and src["sha256"] in r.getMessage() and "version=t-1" in r.getMessage() for r in caplog.records)
    finally:
        app.stop()


def test_no_rules_file_means_provisional_default_unless_switched_off(tmp_path):
    app = _app(tmp_path / "x", SURVEY_RISK_DEFAULT_RULES="off")
    try:
        assert app.readiness()[1]["staff_alert_source"]["active"] is False and app.orchestrator.risk_source is None
    finally:
        app.stop()
    app = _app(tmp_path / "y")
    try:
        assert app.readiness()[1]["staff_alert_source"]["provisional"] is True
    finally:
        app.stop()


def test_bad_rules_file_refuses_to_start(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{}")
    with pytest.raises(ConfigError):
        _app(tmp_path, SURVEY_RISK_RULES_FILE=str(f))
    with pytest.raises(ConfigError):
        _app(tmp_path / "b", SURVEY_RISK_RULES_FILE=str(tmp_path / "missing.json"))
    with pytest.raises(ConfigError):
        Settings.from_env(_env(tmp_path, SURVEY_RISK_RULES_FILE=str(f), SURVEY_RISK_SOURCE="a:b"))


def test_demo_rules_file_is_valid_and_marked_test_only():
    import pathlib
    raw = pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "risk-rules.demo.json").read_bytes()
    src = RuleRiskSource(raw)
    assert "시험용, 임상 규칙 아님" in json.loads(raw)["_comment"] and len(src.rules) == 1 and src.match(TEST_PHRASE) == "demo-test-phrase"


# ---------------------------------------------------------------- 위반 코드 관측
def test_violation_codes_are_counted_and_logged_without_values(caplog):
    rig = Rig()
    rig.start()
    rig.model.queue += [ext("NEXT", "Q1.D02", val("Q1.D01.shoe_size", 270123)), ext("NEXT", "Q1.D02", val("Q1.D01.shoe_size", 270123))]
    with caplog.at_level(logging.INFO, logger="survey_service"):
        o = rig.say("제 신발은 270123 입니다")
    assert o.orchestrator_output["turn_failure_signal"] is not None
    stats = rig.orch.violation_stats()
    assert stats["by_code"] == {"UNKNOWN_SLOT": 2} and stats["total"] == 2 and stats["validations_with_violation"] == 2
    msgs = [r.getMessage() for r in caplog.records]
    fail = [m for m in msgs if "step=VALIDATE" in m]
    assert fail and "UNKNOWN_SLOT@" in fail[0] and "error=RuntimeError" in fail[0]
    assert any("의미 검증 위반" in m and "UNKNOWN_SLOT" in m for m in msgs)
    assert not any("270123" in m or "신발" in m for m in msgs)


def test_readyz_exposes_violation_counters(tmp_path):
    app = _app(tmp_path)
    try:
        app.orchestrator.count_violations(["UNKNOWN_SLOT", "UNKNOWN_SLOT", "NEXT_ITEM_MISMATCH"])
        assert app.readiness()[1]["validation_violations"]["by_code"] == {"UNKNOWN_SLOT": 2, "NEXT_ITEM_MISMATCH": 1}
    finally:
        app.stop()


# ---------------------------------------------------------------- 타임아웃 분류
def _bad(reason=None, status=None):
    d = {k: v for k, v in (("reason", reason), ("status", status)) if v is not None}
    return BoundaryError("UNAVAILABLE", "x", d or None)


def test_timeout_classification():
    assert _timed_out(_bad("TimeoutError"), 0.1, 4.0)                  # 읽기 타임아웃은 걸린 시간과 무관
    assert _timed_out(_bad("URLError"), 3.7, 4.0)                      # 연결 단계 타임아웃(URLError로 감싸짐)은 시간으로 가른다
    assert not _timed_out(_bad("URLError"), 0.01, 4.0)                 # 바로 돌아온 연결 거부
    assert not _timed_out(_bad(status=502), 5.65, 4.0)                 # 느렸어도 서버가 응답을 준 502는 타임아웃이 아니다
    assert not _timed_out(_bad(status=503), 4.0, 4.0)
    assert not _timed_out(_bad("RELOADING"), 5.65, 4.0)                # 다시 적재 대기
    assert not _timed_out(_bad("ConnectionResetError"), 5.0, 4.0)
    assert not _timed_out(_bad(), 5.0, 4.0)


# ---------------------------------------------------------------- 결과 전달 대상 없음
def test_unattached_dispatcher_is_a_status_not_an_error(caplog):
    rig = Rig()
    from survey_service.wiring.ports import UnattachedDispatcher
    rig.orch.dispatcher = UnattachedDispatcher()
    rig.start()
    with caplog.at_level(logging.INFO, logger="survey_service"):
        o = rig.say(kind="SESSION_END")
    assert o.orchestrator_output["turn_failure_signal"] is None and rig.doc()["dispatch_log"] == [{"succeeded": False}]
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert any("결과 전달 대상 없음" in m and lv == logging.INFO for lv, m in msgs)
    assert not any("결과 전달 실패" in m or "RuntimeError" in m for _, m in msgs)
    st = rig.orch.dispatcher_status()
    assert st["attached"] is False and st["status"] == "NO_TARGET" and st["skipped_turns"] == 1


def test_failing_attached_dispatcher_is_still_an_error(caplog):
    rig = Rig()
    rig.dispatcher.fail = True
    rig.start()
    with caplog.at_level(logging.WARNING, logger="survey_service"):
        rig.say(kind="SESSION_END")
    assert any("결과 전달 실패" in r.getMessage() for r in caplog.records)
    assert rig.orch.dispatcher_status() == {"attached": True}
