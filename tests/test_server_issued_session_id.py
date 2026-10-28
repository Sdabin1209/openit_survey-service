"""단계 B (1): 세션 ID는 서버가 발급한다. `POST /v1/sessions` 본문에서 `session_id`를 생략하면 서버가 CSPRNG로 만든 값을 응답의 `session_id`로 돌려준다.

기대값의 출처: ../boundary-contracts/PUBLIC_API.md 7행(2.2.0, D106): 문자열 1~64자·영문·숫자·`-_`, 예측할 수 없는 값(OWASP 세션 관리 권고, 원문 대조 전),
프론트가 session_id를 보내는 방식은 2.x에서만 허용하는 폐기 예정 방식(수용하되 로그에 폐기 예정 경고 한 줄).
CSPRNG 여부는 출력만으로 증명할 수 없다: 시험은 길이(128비트 이상을 base64url로 쓴 22자 이상), 유일성, `secrets` 모듈 사용는 secrets 모듈 호출의 결과와 같음을 본다.
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
    assert ID.match(sid) and len(sid) >= 22
    # 발급된 ID로 이어서 말할 수 있다
    ar.rig.model.queue.append(__import__("tests.e2e_support", fromlist=["ANSWER_OUT"]).ANSWER_OUT)
    code, body, _ = ar.call("POST", f"/v1/sessions/{sid}/turns", {"session_id": sid, "kind": "UTTERANCE", "utterance": "아니요", "turn_id": "t-1"}, auth=AUTH)
    assert code == 200
    assert ar.call("GET", f"/v1/sessions/{sid}/state", auth=AUTH)[0] == 200


def test_issued_ids_are_unique_and_come_from_the_secrets_module(ar, monkeypatch):
    import secrets
    real, made = secrets.token_urlsafe, []

    def spy(n=None):
        v = real(n)
        made.append(v)
        return v
    monkeypatch.setattr(secrets, "token_urlsafe", spy)      # CSPRNG(secrets)에서 왔는지: 발급된 값이 이 호출의 결과와 같다
    ids = []
    for _ in range(60):
        code, body, _ = ar.call("POST", "/v1/sessions", {"kind": "SESSION_START"}, auth=AUTH)
        assert code == 200
        ids.append(body["session_id"])
    assert ids == made and len(set(ids)) == 60


def test_client_supplied_session_id_still_works_with_one_deprecation_warning(ar, caplog):
    caplog.set_level(logging.DEBUG)
    code, body, _ = ar.call("POST", "/v1/sessions", {"session_id": "legacy-sess-1", "kind": "SESSION_START"}, auth=AUTH)
    assert code == 200 and "session_id" not in body        # 프론트가 정한 ID는 응답에 되돌리지 않는다(2.x 호환 모양 그대로)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "폐기 예정" in r.getMessage()]
    assert len(warns) == 1 and "legacy-sess-1" not in warns[0].getMessage()


def test_issuing_does_not_warn(ar, caplog):
    caplog.set_level(logging.DEBUG)
    ar.call("POST", "/v1/sessions", {"kind": "SESSION_START"}, auth=AUTH)
    assert not [r for r in caplog.records if "폐기 예정" in r.getMessage()]


def test_invalid_client_supplied_id_is_still_rejected(ar):
    assert ar.call("POST", "/v1/sessions", {"session_id": "bad id!", "kind": "SESSION_START"}, auth=AUTH)[0] == 400
    assert ar.call("POST", "/v1/sessions", {"session_id": 5, "kind": "SESSION_START"}, auth=AUTH)[0] == 400
    assert ar.call("POST", "/v1/sessions", {"kind": "UTTERANCE"}, auth=AUTH)[0] == 400


def test_issued_session_is_bound_to_the_creating_device(tmp_path):
    from survey_service.api import PublicApi
    import json
    r = AlertRig(tmp_path)
    try:
        api = PublicApi(r.rig.orch, None, device_tokens={"dev-a": "tok-device-a-0123456789", "dev-b": "tok-device-b-0123456789"},
                        staff_token="stok", alerts=r.service)
        code, body, _ = api.handle("POST", "/v1/sessions", "Bearer tok-device-a-0123456789", json.dumps({"kind": "SESSION_START"}).encode())
        assert code == 200
        sid = body["session_id"]
        assert api.handle("GET", f"/v1/sessions/{sid}/state", "Bearer tok-device-a-0123456789", b"")[0] == 200
        assert api.handle("GET", f"/v1/sessions/{sid}/state", "Bearer tok-device-b-0123456789", b"")[0] == 404
    finally:
        r.store.close()
