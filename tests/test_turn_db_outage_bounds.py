"""환자 턴의 DB 장애 시간 상한(FAULT_POLICY 원칙 1·3·4): 연결 거부는 빠르게 503 UNAVAILABLE, 무응답(pause)은 턴 마감 안에 끝난다.
불변식: 키오스크 대기 10초 > gateway 9.5초(연결 1 + 응답 헤더 8.5) > 서버 턴 마감(기본 6초). TEST_DATABASE_URL이 없으면 SKIP."""
import json
import os
import subprocess
import time

import pytest

from survey_service.wiring import pg
from survey_service.components.link_public_api import PublicApi
from survey_service.settings import Settings
from survey_service.components.session_store import PgSessionStore

from .e2e_support import Rig
from .pg_support import pg_url
from . import pg_container as pc

REFUSED = "postgresql://u:p-canary-55@127.0.0.1:1/d"        # 닫힌 포트: 연결 거부
BASE = {"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake"}


def _rig(request, deadline=None, url=None):
    s = Settings.from_env(BASE)
    pg.configure(connect_timeout=s.db_connect_timeout_seconds, statement_timeout=s.db_statement_timeout_seconds,
                 reconnect_backoff=s.db_reconnect_backoff_seconds)
    store = PgSessionStore(url or pg_url(request))
    request.addfinalizer(store.close)
    rig = Rig(store=store, turn_deadline_seconds=s.turn_deadline_seconds if deadline is None else deadline)
    api = PublicApi(rig.orch, "tok", max_utterance_chars=100)
    rig.start("K1")
    return rig, api, store, s


def _turn(api, tid):
    body = {"session_id": "K1", "kind": "UTTERANCE", "utterance": "안 피움", "turn_id": tid}
    t = time.monotonic()
    code, resp, hdr = api.handle("POST", "/v1/sessions/K1/turns", "Bearer tok", json.dumps(body, ensure_ascii=False).encode())
    return code, resp, time.monotonic() - t


def test_defaults_keep_the_timeout_ordering():
    s = Settings.from_env(BASE)
    # 키오스크 10 > gateway 9.5(연결 1 + 헤더 8.5) > 서버 마감. 마감 직전에 시작한 접속 시도(connect)가 끝나는 시간도 gateway 상한 안에 든다
    assert s.turn_deadline_seconds + 0.5 < 8.5 and s.turn_deadline_seconds <= 7
    assert s.db_connect_timeout_seconds + s.db_statement_timeout_seconds + 1 < s.turn_deadline_seconds + 1


def test_connection_refused_ends_fast_with_503_then_recovers(request):
    rig, api, store, s = _rig(request)
    good = store._db._url
    store._db._conn.close()
    store._db._url = REFUSED
    code, resp, dt = _turn(api, "t-refused")
    assert code == 503 and resp["code"] == "UNAVAILABLE" and resp["retryable"] is True and dt < s.db_connect_timeout_seconds + 1, dt
    assert "p-canary-55" not in json.dumps(resp)
    code, resp, dt = _turn(api, "t-refused-2")                  # 접속 실패 직후(재접속 유예 중)에는 접속을 시도하지 않고 바로 실패한다
    assert code == 503 and dt < 0.5, dt
    store._db._url = good
    time.sleep(s.db_reconnect_backoff_seconds + 0.2)
    rig.model.queue.append({"action": "NEXT", "next_item": "END", "values": []})
    assert _turn(api, "t-refused-3")[0] == 200                  # 복구


def test_frozen_db_ends_within_server_turn_deadline(request):
    if not pc.docker_ready():
        pytest.skip("docker 또는 postgres:16 이미지 없음: 일회용 PostgreSQL을 띄울 수 없음")
    cid, url = pc.start_postgres()          # 이 시험이 띄운 컨테이너만 이름으로 멈춘다(ADR-0019)
    request.addfinalizer(lambda: pc.stop(cid))
    rig, api, store, s = _rig(request, url=url)
    pc.pause(cid)
    try:
        code, resp, dt = _turn(api, "t-frozen")
        # 어느 경로든(사전 확인의 503 또는 턴 마감의 NOT_COMMITTED) 서버 턴 마감 안에, gateway 응답 헤더 상한(8.5초)보다 충분히 짧게 끝난다
        assert dt < s.turn_deadline_seconds + 0.5 and dt < 7.0, dt
        assert code == 503 or resp["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    finally:
        pc.unpause(cid)
