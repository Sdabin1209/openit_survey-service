"""환자 턴의 DB 장애 시간 상한(FAULT_POLICY 원칙 1·3·4): 연결 거부는 즉시 503 UNAVAILABLE. 무응답 DB(docker pause)로 접속·문장 시한을 자르는 것은 libpq·서버의 몫이라 시험하지 않는다(U421): 시한 값은 tests/test_readyz_db_outage.py가, 턴 마감 처리는 tests/test_turn_idempotency_deadline.py가 본다. 마감 안에 끝난다.
불변식: 키오스크 대기 12초 > gateway 10.5초 > 서버 턴 마감(기본 9초, ADR-0049). TEST_DATABASE_URL이 없으면 SKIP."""
import json
import time

import pytest

from .timing import fake_clock
from survey_service.components import session_store as pg
from survey_service.components.link_public_api import PublicApi
from survey_service.settings import Settings
from survey_service.components.session_store import PgSessionStore

from .e2e_support import Rig
from .pg_support import pg_url

REFUSED = "postgresql://u:p-canary-55@127.0.0.1:1/d"        # 닫힌 포트: 연결 거부
BASE = {"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake"}


def _rig(request, deadline=None, url=None):
    s = Settings.from_env(BASE)
    pg.configure(connect_timeout=s.db_connect_timeout_seconds, statement_timeout=s.db_statement_timeout_seconds,
                 reconnect_backoff=0.0)      # 접속 실패 뒤 재접속 유예는 시간 기반이라 시험에서는 0으로 둔다(유예 값이 설정에서 오는지는 test_readyz_db_outage의 설정 확인이 본다)
    store = PgSessionStore(url or pg_url(request), clock=fake_clock())
    request.addfinalizer(store.close)
    rig = Rig(store=store, turn_deadline_seconds=s.turn_deadline_seconds if deadline is None else deadline)
    api = PublicApi(rig.orch, "tok", max_utterance_chars=100)
    rig.start("K1")
    return rig, api, store, s


def _turn(api, tid):
    body = {"session_id": "K1", "kind": "UTTERANCE", "utterance": "안 피움", "turn_id": tid}
    code, resp, hdr = api.handle("POST", "/v1/sessions/K1/turns", "Bearer tok", json.dumps(body, ensure_ascii=False).encode())
    return code, resp


def test_defaults_keep_the_timeout_ordering():
    s = Settings.from_env(BASE)
    # 키오스크 12 > gateway 10.5(연결 1 + 헤더 9.5) > 서버 마감 9(ADR-0049). 마감 직전에 시작한 접속 시도(connect)가 끝나는 시간도 gateway 상한 안에 든다
    assert s.turn_deadline_seconds + 0.5 <= 9.5 and s.turn_deadline_seconds <= 9
    assert s.db_connect_timeout_seconds + s.db_statement_timeout_seconds + 1 < s.turn_deadline_seconds + 1


def test_connection_refused_ends_fast_with_503_then_recovers(request):
    rig, api, store, s = _rig(request)
    good = store._db._url
    store._db._conn.close()
    store._db._url = REFUSED
    code, resp = _turn(api, "t-refused")
    assert code == 503 and resp["code"] == "UNAVAILABLE" and resp["retryable"] is True      # 연결 거부는 즉시 오류다(시간은 재지 않는다)
    assert "p-canary-55" not in json.dumps(resp)
    code, resp = _turn(api, "t-refused-2")                  # 접속 실패 직후(재접속 유예 중)에도 같은 503이다
    assert code == 503
    store._db._url = good
    rig.model.queue.append({"action": "NEXT", "next_item": "END", "values": []})
    assert _turn(api, "t-refused-3")[0] == 200                  # 복구
