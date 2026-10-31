"""U26: 결과 목록(GET /v1/results)의 순서와 커서는 벽시계(updated_at)가 아니라 DB가 쓸 때마다 매기는 단조 증가 번호(update_seq)를 따른다 (ADR-0034).
이 PC(WSL2)에서 벽시계는 120초에 2번, 최대 155.7ms 되감긴다(헤드 측정). 되감겨도 쓴 순서대로 나와야 하고, 커서가 쪽 사이에서 행을 빠뜨리거나 되풀이하지 않아야 한다."""
import itertools
import sqlite3

import pytest

from .timing import fake_clock
from .test_results_signals_retention import RESULT, env  # noqa: F401  (env는 sqlite와 postgres를 도는 픽스처)


def ids(body):
    return [i["session_id"] for i in body["items"]]


def get(env, q=""):
    st, body, _ = env.call("GET", "/v1/results" + q, auth=RESULT)
    assert st == 200, (st, body)
    return body


def test_list_follows_write_order_when_the_wall_clock_runs_backwards(env):
    clock = itertools.count(10_000.0, -1.0)                       # 쓸 때마다 시각이 1초씩 줄어든다
    env.rig.store._clock = lambda: next(clock)
    for sid in ("K1", "K2", "K3"):
        env.start(sid)
    assert ids(get(env)) == ["K1", "K2", "K3"]                    # 시각순이면 K3, K2, K1
    env.say("K1", tid="t-2")                                      # K1에 새 기록: 마지막 쓴 세션이 된다
    assert ids(get(env)) == ["K2", "K3", "K1"]


def test_cursor_pages_do_not_skip_or_repeat_rows_when_the_clock_runs_backwards(env):
    clock = itertools.count(10_000.0, -1.0)
    env.rig.store._clock = lambda: next(clock)
    for sid in ("K1", "K2", "K3", "K4", "K5"):
        env.start(sid)
    seen, cur = [], None
    while True:
        b = get(env, "?limit=2" + (f"&cursor={cur}" if cur else ""))
        seen += ids(b)
        cur = b["next_cursor"]
        if not cur:
            break
    assert seen == ["K1", "K2", "K3", "K4", "K5"]


def test_equal_clock_values_keep_write_order(env):
    env.rig.store._clock = lambda: 1_700_000_000.0               # 모든 쓰기가 같은 시각이어도 쓴 순서가 순서를 정한다(session_id가 아니다)
    for sid in ("K3", "K1", "K2"):
        env.start(sid)
    assert ids(get(env)) == ["K3", "K1", "K2"]


def test_malformed_cursor_is_400(env):
    for bad in ("zz", "1.5|K1", "-1", "99999999999999999999999"):
        st, _, _ = env.call("GET", "/v1/results?cursor=" + bad, auth=RESULT)
        assert st == 400, bad


def test_pre_u26_sqlite_file_is_backfilled_in_the_old_order(tmp_path):
    from survey_service.components.session_store import SqliteSessionStore
    p = str(tmp_path / "old.db")
    with sqlite3.connect(p) as db:
        db.executescript("""
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, release_id TEXT NOT NULL, status TEXT NOT NULL, doc TEXT NOT NULL, updated_at REAL NOT NULL DEFAULT 0);
CREATE TABLE turns (session_id TEXT NOT NULL, turn_id TEXT NOT NULL, input_hash TEXT NOT NULL, committed INTEGER NOT NULL DEFAULT 0, result_json TEXT, PRIMARY KEY (session_id, turn_id));
INSERT INTO sessions VALUES ('B', 1, 'r', 'IN_PROGRESS', '{}', 5.0), ('A', 1, 'r', 'IN_PROGRESS', '{}', 5.0), ('C', 1, 'r', 'IN_PROGRESS', '{}', 3.0);""")
    store = SqliteSessionStore(p, clock=fake_clock())
    try:
        assert [r[0] for r in store.list_sessions()] == ["C", "A", "B"]       # updated_at, 같으면 session_id (옛 순서)
    finally:
        store.close()
