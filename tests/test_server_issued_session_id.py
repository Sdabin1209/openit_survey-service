"""단계 B (1): 세션 ID는 서버가 발급한다. `POST /v1/sessions` 본문에서 `session_id`를 생략하면 서버가 CSPRNG로 만든 값을 응답의 `session_id`로 돌려준다.

기대값의 출처: ../boundary-contracts/PUBLIC_API.md 7행(2.2.0, D106): 문자열 1~64자·영문·숫자·`-_`, 예측할 수 없는 값(OWASP 세션 관리 권고, 원문 대조 전),
4.0.0 공개 HTTP 시작 본문은 session_id를 거부한다. 내부 FrontendMessage와 구분한다.
CSPRNG 여부는 출력만으로 증명할 수 없다: 시험은 길이(256비트를 16진으로 쓴 64자), 유일성, `secrets` 모듈 사용는 secrets 모듈 호출의 결과와 같음을 본다.
"""
import logging
import re

import pytest

from .test_staff_alerts import AUTH, AlertRig

ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@pytest.fixture
def ar(tmp_path):
    r = AlertRig(tmp_path)
    yield r
    r.store.close()


def test_omitted_session_id_is_issued_by_the_server(ar):
    code, body, _ = ar.call("POST", "/v1/sessions", {"kind": "SESSION_START"}, auth=AUTH)
    assert code == 200
    sid = body["session_id"]
    assert ID.match(sid) and len(sid) == 64
    # 발급된 ID로 이어서 말할 수 있다
    ar.rig.model.queue.append(__import__("tests.e2e_support", fromlist=["ANSWER_OUT"]).ANSWER_OUT)
    code, body, _ = ar.call("POST", f"/v1/sessions/{sid}/turns", {"session_id": sid, "kind": "UTTERANCE", "utterance": "아니요", "turn_id": "t-1"}, auth=AUTH)
    assert code == 200
    assert ar.call("GET", f"/v1/sessions/{sid}/state", auth=AUTH)[0] == 200


def test_issued_ids_are_unique_and_come_from_the_secrets_module(ar, monkeypatch):
    import secrets
    real, made = secrets.token_hex, []

    def spy(n=None):
        v = real(n)
        made.append(v)
        return v
    monkeypatch.setattr(secrets, "token_hex", spy)      # CSPRNG(secrets)에서 왔는지: 발급된 값이 이 호출의 결과와 같다
    ids = []
    for _ in range(60):
        code, body, _ = ar.call("POST", "/v1/sessions", {"kind": "SESSION_START"}, auth=AUTH)
        assert code == 200
        ids.append(body["session_id"])
    assert ids == made and len(set(ids)) == 60


def test_client_supplied_session_id_is_rejected_without_creating_session(ar):
    code, body, _ = ar.call("POST", "/v1/sessions", {"session_id": "legacy-sess-1", "kind": "SESSION_START"}, auth=AUTH)
    assert code == 400 and body["code"] == "INVALID_RECORD"
    assert ar.call("GET", "/v1/sessions/legacy-sess-1/state", auth=AUTH)[0] == 404


def test_issuing_does_not_warn(ar, caplog):
    caplog.set_level(logging.DEBUG)
    ar.call("POST", "/v1/sessions", {"kind": "SESSION_START"}, auth=AUTH)
    assert not [r for r in caplog.records if "폐기 예정" in r.getMessage()]


def test_invalid_client_supplied_id_is_still_rejected(ar):
    assert ar.call("POST", "/v1/sessions", {"session_id": "bad id!", "kind": "SESSION_START"}, auth=AUTH)[0] == 400
    assert ar.call("POST", "/v1/sessions", {"session_id": 5, "kind": "SESSION_START"}, auth=AUTH)[0] == 400
    assert ar.call("POST", "/v1/sessions", {"kind": "UTTERANCE"}, auth=AUTH)[0] == 400


def test_issued_session_is_bound_to_the_creating_device(tmp_path):
    from survey_service.components.link_public_api import PublicApi
    import json
    r = AlertRig(tmp_path)
    try:
        api = PublicApi(r.rig.orch, "gateway-token-0123456789",
                        staff_token="stok", alerts=r.service)
        code, body, _ = api.handle("POST", "/v1/sessions", "Bearer gateway-token-0123456789", json.dumps({"kind": "SESSION_START"}).encode(), device="dev-a")
        assert code == 200
        sid = body["session_id"]
        assert api.handle("GET", f"/v1/sessions/{sid}/state", "Bearer gateway-token-0123456789", b"", device="dev-a")[0] == 200
        assert api.handle("GET", f"/v1/sessions/{sid}/state", "Bearer gateway-token-0123456789", b"", device="dev-b")[0] == 404
    finally:
        r.store.close()


def test_schemas_describe_the_start_request_and_response():
    """스키마가 선택적 session_id(요청)와 시작 응답의 최상위 session_id를 기술한다(PUBLIC_API.md 7행). 위치: schema/schemas/FrontendMessage.json(요청), FrontendInput.json(응답)."""
    import jsonschema
    import survey_schema
    msg = jsonschema.Draft202012Validator(survey_schema.load_schema("FrontendMessage"))
    fi = jsonschema.Draft202012Validator(survey_schema.load_schema("FrontendInput"))
    assert not list(msg.iter_errors({"kind": "SESSION_START"}))                                   # 서버 발급: session_id 생략
    assert not list(msg.iter_errors({"kind": "SESSION_START", "session_id": "legacy-1"}))  # 내부 메시지
    from boundary_contracts import validator
    start = validator("PublicApiRequests#/$defs/SessionStartBody")
    assert start.is_valid({"kind": "SESSION_START"})
    assert not start.is_valid({"kind": "SESSION_START", "session_id": "legacy-1"})
    assert list(msg.iter_errors({"kind": "UTTERANCE", "utterance": "x"}))                         # 다른 kind는 session_id 필수
    assert list(msg.iter_errors({"kind": "SESSION_END"}))
    base = {"response_text": "안녕하세요", "turn_failure_signal": None}
    assert not list(fi.iter_errors(base))
    assert not list(fi.iter_errors({**base, "session_id": "a" * 43}))
    assert list(fi.iter_errors({**base, "session_id": ""}))
    assert list(fi.iter_errors({**base, "session_id": "has space"}))


def test_issued_start_response_validates_against_the_frontend_input_schema(ar):
    import jsonschema
    import survey_schema
    fi = jsonschema.Draft202012Validator(survey_schema.load_schema("FrontendInput"))
    code, body, _ = ar.call("POST", "/v1/sessions", {"kind": "SESSION_START"}, auth=AUTH)
    assert code == 200 and not list(fi.iter_errors(body))


# ---- ADR-0066: 발급 ID 는 공개 SessionId 와 계약 Id 를 동시에 통과한다 ----
def _public_session_ok(sid):
    from boundary_contracts import validator
    return validator("PublicApiRequests#/$defs/UtteranceBody").is_valid({"session_id": sid, "kind": "UTTERANCE", "utterance": "x", "turn_id": "t-1"})


def _contract_id_ok(sid):
    from boundary_contracts import validator
    return validator("_common#/$defs/Id").is_valid(sid)


def _result_ready_errors(sid):
    from boundary_contracts import validator
    return list(validator("ResultReadyEvent").iter_errors({"session_id": sid, "status": "ENDED_COMPLETED", "ended_at": "2026-11-08T00:00:00Z"}))


def test_many_issued_ids_pass_both_the_public_session_id_and_the_contract_id(ar):
    """표본 400개: token_urlsafe 였다면 약 3%가 첫 글자 '_'·'-' 라 Id 를 어긴다(확률 2/64). 16진은 어긴 적이 없다."""
    ids = []
    for _ in range(400):
        code, body, _ = ar.call("POST", "/v1/sessions", {"kind": "SESSION_START"}, auth=AUTH)
        assert code == 200
        ids.append(body["session_id"])
    assert all(_public_session_ok(s) and _contract_id_ok(s) for s in ids)
    assert all(len(s) == 64 and re.fullmatch(r"[0-9a-f]{64}", s) for s in ids) and len(set(ids)) == 400


def test_result_ready_event_of_an_issued_id_validates_against_the_contract(ar):
    """DTO 검증이다(실제 SSE 경로가 아니다): 발급값으로 ResultReadyEvent 본문을 직접 만들어 계약 스키마로 본다. 실제 SSE 경로는 tests/test_sse_c2.py 의 발급 ID 시험이 본다."""
    code, body, _ = ar.call("POST", "/v1/sessions", {"kind": "SESSION_START"}, auth=AUTH)
    assert code == 200 and _result_ready_errors(body["session_id"]) == []


@pytest.mark.parametrize("bad", ["-" + "a" * 42, "_" + "a" * 42])
def test_an_id_starting_with_dash_or_underscore_breaks_result_ready_event(bad):
    """계약 사실 확인: token_urlsafe 가 낼 수 있는 이런 ID 는 5.0.0 부터 공개 SessionId(Id 와의 교집합)와 Id, ResultReadyEvent 모두 위반한다(4.x 에서는 공개 SessionId 만 통과했다)."""
    assert not _public_session_ok(bad) and not _contract_id_ok(bad) and _result_ready_errors(bad)


def test_a_bad_issued_value_fails_the_result_ready_check(ar, monkeypatch):
    """검사 민감도 시험(token_urlsafe 되돌림 변이가 아니다): token_hex 가 '-' 로 시작하는 값을 내도록 주입하면 발급값에 대한 ResultReadyEvent 검증이 실패한다.
    되돌림 변이 자체는 표본 시험(16진 64자 단언)이 잡는다."""
    import secrets
    monkeypatch.setattr(secrets, "token_hex", lambda n=None: "-" + "a" * 42)       # token_urlsafe 가 낼 수 있는 값의 예
    code, body, _ = ar.call("POST", "/v1/sessions", {"kind": "SESSION_START"}, auth=AUTH)
    assert code == 200 and _result_ready_errors(body["session_id"])


# ---- envelope_id: 계약 Id 와 같은 판정(ops-feedback 과 같은 입력 표) ----
@pytest.mark.parametrize("raw,keeps", [
    ("abc", True), ("abc\n", False), ("abc\r\n", False), ("a" * 128, True), ("a" * 129, False), ("", False),
    ("_abc", False), ("-abc", False), ("a_b-c.d:e", True), ("가나다", False), ("abc가", False), ("ａｂｃ", False), (" abc", False), ("abc ", False), ("0abc", True),
])
def test_envelope_id_follows_the_contract_id_exactly(raw, keeps):
    """Id 이면 그대로, 아니면 x-+SHA-256 앞 32자. 판정은 계약 Id 검증기와 같아야 한다(끝 줄바꿈·128/129자·비ASCII 경계). 같은 표를 ops-feedback 시험도 쓴다."""
    import hashlib
    from boundary_contracts import envelope_id
    assert _contract_id_ok(raw) is keeps
    out = envelope_id(raw)
    assert (out == raw) if keeps else (out == "x-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32])
    assert _contract_id_ok(out)
