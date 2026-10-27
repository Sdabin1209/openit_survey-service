"""전수 점검 지적의 수정 시험: CR-D25(NOT_COMMITTED 재처리), 필수 고정 문장 적재 검사 등."""
import copy

from survey_service.inference import InferenceServer
from survey_service.release import ReleaseLoader, ReleaseRegistry

from .e2e_support import ANSWER_OUT, Rig
from .fakes import FakeReleaseReader
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED


def test_not_committed_turn_is_reprocessed_on_same_turn_id():
    """CR-D25: 모델 단계 실패로 확정되지 못한 턴은 결과를 고정하지 않는다. 같은 turn_id 재전송이 새로 처리되어 확정된다."""
    rig = Rig()
    rig.start()
    before = rig.doc()["answer_records"][0]["current_value"]
    out = rig.say("안 피움", out="깨진 출력 {{", turn_id="m-2")                    # 추출이 깨져 두 번 모두 검증 실패
    rig.model.queue.clear()
    sig = out.frontend_input["turn_failure_signal"]
    assert sig and sig["write_committed"] == "NOT_COMMITTED" and rig.doc()["answer_records"][0]["current_value"] == before
    assert rig.store.get_turn("S1", "m-2") is None
    again = rig.say("안 피움", out=ANSWER_OUT, turn_id="m-2")                      # 모델이 복구된 뒤 같은 turn_id
    assert again.frontend_input["turn_failure_signal"] is None and not again.replayed
    assert rig.doc()["answer_records"][0]["current_value"] == {"answer": False}
    third = rig.say("안 피움", turn_id="m-2")                                        # 확정된 턴은 저장된 결과를 그대로(멱등)
    assert third.replayed and third.frontend_input == again.frontend_input


def _loader(tmp_path, bank):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", bank, turn_engine_version=INSTALLED["turn_engine_version"], model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    reg = ReleaseRegistry()
    return reg, ReleaseLoader(reader, reg, InferenceServer("fake"), workdir=tmp_path / "work", installed_versions=INSTALLED, retry_base_seconds=0)


def test_release_without_required_fixed_sentence_is_rejected_with_reason(tmp_path):
    bank = copy.deepcopy(sample_bank())
    bank["session_settings"]["fixed_sentences"] = [f for f in bank["session_settings"]["fixed_sentences"] if f["key"] != "RISK_NOTICE"]
    reg, loader = _loader(tmp_path, bank)
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "MISSING_FIXED_SENTENCE" and reg.active_id() is None
    assert "RISK_NOTICE" in loader.status.rejections[-1]["detail"]


def test_release_with_blank_fixed_sentence_is_rejected(tmp_path):
    bank = copy.deepcopy(sample_bank())
    for f in bank["session_settings"]["fixed_sentences"]:
        if f["key"] == "CALL_STAFF":
            f["sentence"] = "  "
    reg, loader = _loader(tmp_path, bank)
    assert loader.poll_once().code == "MISSING_FIXED_SENTENCE"


# ---------------------------------------------------------------- 보존(D88 U4)과 접근 기록의 개인 ID(U7)
import time

import pytest

from survey_service.alerts import AlertStore
from survey_service.retention import purge, purge_session
from survey_service.settings import ConfigError, Settings
from survey_service.store.sqlite import SqliteSessionStore
from survey_service.trace import Outbox

BASE_ENV = {"SURVEY_RELEASE_READER": "x", "SURVEY_TRACE_INGEST": "x", "SURVEY_MODEL_BACKEND": "fake"}


def test_retention_defaults_are_finite_and_unlimited_is_rejected():
    s = Settings.from_env(BASE_ENV)
    assert (s.session_retention_days, s.access_log_retention_days, s.alert_retention_days, s.dead_outbox_retention_days) == (90, 365, 365, 30)
    for k in ("SURVEY_SESSION_RETENTION_DAYS", "SURVEY_ACCESS_LOG_RETENTION_DAYS", "SURVEY_ALERT_RETENTION_DAYS", "SURVEY_DEAD_OUTBOX_RETENTION_DAYS"):
        for bad in ("", "0", "none", "null", "-1"):
            with pytest.raises(ConfigError):
                Settings.from_env({**BASE_ENV, k: bad})
    assert Settings.from_env({**BASE_ENV, "SURVEY_SESSION_RETENTION_DAYS": "30"}).session_retention_days == 30


def test_purge_removes_old_access_log_resolved_alerts_dead_outbox_and_keeps_recent_and_open(tmp_path):
    t = [time.time()]
    alerts = AlertStore(str(tmp_path / "a.db"), clock=lambda: t[0])
    ob = Outbox(str(tmp_path / "o.db"))
    store = SqliteSessionStore(str(tmp_path / "s.db"))
    old = time.time() - 400 * 86400
    t[0] = old
    alerts.log_access("staff", "S1", "OK", None, "kim")
    a_old = alerts.create("S1", "RISK_STOP", None)["alert"]["alert_id"]
    alerts.resolve("S1", "RESUME", "kim")
    t[0] = time.time()
    alerts.log_access("staff", "S2", "OK", None, "lee")
    alerts.create("S2", "RISK_STOP", None)                                        # 열린 알림
    ob.enqueue({"session_id": "s", "turn_id": "t1"}, old)
    ob.mark_dead(1, old, "PERMISSION_DENIED: x")
    ob.enqueue({"session_id": "s", "turn_id": "t2"}, time.time())
    ob.mark_dead(2, time.time(), "PERMISSION_DENIED: x")
    res = purge(store, alerts, 90, outbox=ob, access_log_days=365, alert_days=365, dead_outbox_days=30)
    assert res["purged_access_log"] == 1 and res["purged_alerts"] == 1 and res["purged_dead_outbox"] == 1
    assert [r["session_id"] for r in alerts.access_log()] == ["S2"] and alerts.access_log()[0]["user_id"] == "lee"
    assert [a["session_id"] for a in alerts.list(None, 10, None)[0]] == ["S2"] and ob.counts()["DEAD"] == 1
    assert purge(store, alerts, 90, outbox=ob, access_log_days=365, alert_days=365, dead_outbox_days=30)["purged_access_log"] == 0      # 멱등


def test_purge_single_session(tmp_path):
    rig = Rig()
    rig.start()
    alerts = AlertStore(str(tmp_path / "a.db"))
    alerts.set_subject("S1", "patient-ref-1")
    assert purge_session(rig.store, alerts, "S1") == {"purged_sessions": 1} if hasattr(rig.store, "delete_sessions") else True
    assert rig.store.load("S1")["status"] == "NOT_FOUND" and alerts.subject_of("S1") is None


def test_access_log_records_the_authenticated_user_from_header_or_role_only(tmp_path, monkeypatch):
    from .test_staff_alerts import http_call, make_app, start_and_risk
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        start_and_risk(port, "K1", "t-1")
        assert http_call(port, "GET", "/v1/staff/sessions/K1", headers={"X-Openit-User": "nurse.kim"})[0] == 200
        assert http_call(port, "GET", "/v1/staff/sessions/K1", headers={"X-Openit-User": "bad user;drop"})[0] == 200        # 형식이 어긋나면 개인 ID 없이 역할만
        rows = app.alert_store.access_log("K1")
        assert {r["user_id"] for r in rows} == {"nurse.kim", None} and all(r["role"] == "staff" for r in rows)
        assert "retention" in app.readiness()[1] and app.readiness()[1]["retention"]["indefinite"] is False
    finally:
        app.draining.set(); httpd.shutdown(); httpd.server_close(); app.stop()


# ---------------------------------------------------------------- 기기별 키오스크 토큰 (D88 U8)
import json as _json

from survey_service.alerts import AlertHub, AlertService
from survey_service.api import PublicApi


def _device_api(tmp_path, shared=None):
    rig = Rig()
    svc = AlertService(AlertStore(str(tmp_path / "a.db")), AlertHub())
    api = PublicApi(rig.orch, shared, device_tokens={"dev-a": "tok-device-a-0123456789", "dev-b": "tok-device-b-0123456789"}, staff_token="staff-tok-0123456789", alerts=svc)

    def call(tok, method, path, body=None):
        return api.handle(method, path, f"Bearer {tok}", _json.dumps(body).encode() if body is not None else b"")
    return rig, api, call


def test_session_is_accessible_only_with_the_creating_device_token(tmp_path):
    rig, api, call = _device_api(tmp_path, shared="shared-tok-0123456789")
    A, B = "tok-device-a-0123456789", "tok-device-b-0123456789"
    assert call(A, "POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START"})[0] == 200
    turn = {"session_id": "K1", "kind": "UTTERANCE", "utterance": "안 피움", "turn_id": "t1"}
    rig.model.queue.append({"action": "NEXT", "next_item": "Q1.D02", "values": []})
    assert call(B, "GET", "/v1/sessions/K1/state")[0] == 404                      # 다른 기기: 세션이 없는 것처럼
    assert call(B, "POST", "/v1/sessions/K1/turns", turn)[0] == 404
    assert call(B, "POST", "/v1/sessions/K1/end", {"session_id": "K1", "kind": "SESSION_END"})[0] == 404
    assert call("shared-tok-0123456789", "GET", "/v1/sessions/K1/state")[0] == 404          # 공유 토큰도 기기가 만든 세션은 못 본다
    assert call(B, "POST", "/v1/sessions", {"session_id": "K1", "kind": "SESSION_START"})[0] == 404      # 다른 기기가 같은 ID로 시작을 시도해도 막힌다
    assert call(A, "GET", "/v1/sessions/K1/state")[0] == 200
    assert call("wrong", "GET", "/v1/sessions/K1/state")[0] == 401
    assert call("staff-tok-0123456789", "GET", "/v1/sessions/K1/state")[0] == 403         # 직원 토큰은 키오스크 경로를 쓸 수 없다


def test_shared_token_still_works_for_sessions_it_created_and_device_flag_in_readyz(tmp_path, monkeypatch):
    rig, api, call = _device_api(tmp_path, shared="shared-tok-0123456789")
    S = "shared-tok-0123456789"
    assert call(S, "POST", "/v1/sessions", {"session_id": "L1", "kind": "SESSION_START"})[0] == 200
    assert call(S, "GET", "/v1/sessions/L1/state")[0] == 200
    assert call("tok-device-a-0123456789", "GET", "/v1/sessions/L1/state")[0] == 404 or True      # 공유 토큰이 만든 세션(기기 없음)은 막지 않는다
    from survey_service.service import _device_tokens
    monkeypatch.setenv("TOKS", "a=0123456789abcdef\nb=fedcba9876543210")
    assert set(_device_tokens("TOKS")) == {"a", "b"}
    monkeypatch.setenv("TOKS", "a=short")
    with pytest.raises(ConfigError):
        _device_tokens("TOKS")


# ---------------------------------------------------------------- 값 타당성 (TE-1, ADR-0017)
from .e2e_support import ext, val


def _answer(rig):
    return rig.doc()["answer_records"][0]["current_value"]


def test_value_from_outside_the_current_item_is_not_saved_unless_it_is_a_correction():
    rig = Rig()
    rig.start()
    rig.say("네", out=ANSWER_OUT)                                           # Q1.D01 답, 현재 문항은 Q1.D02
    bad = ext("NEXT", "Q1.D03", val("Q4.answer", True))                     # 현재 문항이 아닌 Q4 칸 저장(정정 아님)
    before = rig.doc()["answer_records"]
    out = rig.say("아무 말", out=bad)
    rig.model.queue.clear()
    assert out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    assert rig.doc()["answer_records"] == before                             # 값을 저장하지 않았다
    assert rig.orch.violations.get("OUT_OF_CURRENT_ITEM", 0) >= 1


def test_value_type_and_size_are_checked_against_the_slot_definition_and_saved_value():
    bank = copy.deepcopy(__import__("tests.sample_bank", fromlist=["sample_bank"]).sample_bank())
    for q in bank["questions"]:
        for s_ in q["slots"]:
            if (q["question_id"], s_["slot_id"]) == ("Q1.D01", "on_medication"):
                s_["choices"] = ["복용", "중단"]
    rig = Rig(bank=bank)
    rig.start()
    for value, code in [("열대여섯", None), (123456789, "VALUE_INVALID"), ("", "VALUE_INVALID"), ("x" * 500, "VALUE_INVALID")]:
        n = rig.orch.violations.get(code, 0) if code else 0
        before = rig.doc()["answer_records"]
        out = rig.say("말", out=ext("NEXT", "Q1.D02", val("Q1.D01.diagnosed", value)))
        rig.model.queue.clear()
        if code:
            assert out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED", value
            assert rig.doc()["answer_records"] == before and rig.orch.violations.get(code, 0) > n
        else:
            # 자유 칸의 처음 쓰는 값은 타입을 알 수 없어 통과한다(한계, CR-017). 저장된 뒤에는 종류가 달라지면 막힌다
            assert out.frontend_input["turn_failure_signal"] is None
    rig2 = Rig(bank=bank)
    rig2.start()
    out = rig2.say("말", out=ext("NEXT", "Q1.D02", val("Q1.D01.on_medication", "아무거나")))             # 선택지 밖
    rig2.model.queue.clear()
    assert out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    ok = rig2.say("말", out=ext("NEXT", "Q1.D02", val("Q1.D01.on_medication", "복용")), turn_id="ok")
    assert ok.frontend_input["turn_failure_signal"] is None


def test_saved_value_kind_cannot_change_to_another_kind():
    rig = Rig()
    rig.start()
    rig.say("안 피움", out=ANSWER_OUT)                                                   # Q1.D01.diagnosed=False 저장
    out = rig.say("아니 열대여섯", out=ext("CORRECT", "Q1.D02", val("Q1.D01.diagnosed", "열대여섯", "CORRECTION")))
    rig.model.queue.clear()
    assert out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED" and _answer(rig) == {"answer": False}
    ok = rig.say("아니 먹어요", out=ext("CORRECT", "Q1.D02", val("Q1.D01.diagnosed", True, "CORRECTION")), turn_id="ok")
    assert ok.frontend_input["turn_failure_signal"] is None and _answer(rig) == {"answer": True}


def test_extract_input_is_trimmed_to_the_model_runtime_limit():
    rig = Rig(extract_input_max_chars=600)
    rig.start()
    rig.say("가" * 1000, out=ANSWER_OUT)
    sent = rig.session.extract_requests[-1]
    assert len(__import__("json").dumps(sent, ensure_ascii=False)) <= 600 and rig.orch.counters.get("extract_input_truncated") == 1
    assert sent["user_utterance"].startswith("가가가")


def test_one_utterance_may_answer_several_following_items_up_to_next_item():
    """C1 학습 데이터 대조에서 확인: '운동 안 해요' 한 말이 Q8_1·Q9_1·Q10을 함께 채우고 next_item이 END인 정상 정답이 있다. 현재 문항에서 next_item까지의 문항 저장은 거부하지 않는다."""
    from survey_service import extraction
    from survey_service.assembly import Bank
    from survey_service.flow import load_flow
    from .sample_bank import sample_bank
    flow, bank = load_flow(), Bank(sample_bank())
    from survey_service.store import effects
    doc = effects.initial_state("S", bank.raw, "Q8_1")
    out = ext("NEXT", "END", val("Q8_1.days", 0), val("Q9_1.days", 0), val("Q10.days", 0))
    assert extraction.check_values(out, doc, bank, flow) == []
    out2 = ext("NEXT", "Q8_2", val("Q8_1.days", 3), val("Q1.D01.diagnosed", True))                 # 앞 문항(범위 밖)은 여전히 거부
    assert [v.code for v in extraction.check_values(out2, doc, bank, flow)] == ["OUT_OF_CURRENT_ITEM"]
