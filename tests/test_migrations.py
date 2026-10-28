"""스키마 마이그레이션(yoyo, ADR-0030): 새 데이터베이스, 이미 있는 스키마(yoyo 도입 전 코드가 만든 것), 반복 실행, SQLite·PostgreSQL 같은 표."""
import sqlite3

import pytest

from survey_service.alerts import AlertStore
from survey_service.migrate import migrate
from survey_service.store.sqlite import SqliteSessionStore
from survey_service.trace.outbox import Outbox

LEGACY_SESSIONS = """
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, release_id TEXT NOT NULL, status TEXT NOT NULL, doc TEXT NOT NULL, updated_at REAL NOT NULL DEFAULT 0);
CREATE INDEX sessions_status ON sessions(status);
CREATE TABLE turns (session_id TEXT NOT NULL, turn_id TEXT NOT NULL, input_hash TEXT NOT NULL, committed INTEGER NOT NULL DEFAULT 0, result_json TEXT, PRIMARY KEY (session_id, turn_id));
CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, envelope TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','SENT','DEAD')), attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
  last_error TEXT, created_at REAL NOT NULL, finished_at REAL, UNIQUE (session_id, turn_id));
INSERT INTO sessions VALUES ('S1', 3, 'rel-1', 'IN_PROGRESS', '{}', 5.0);
"""


def _cols(path, table):
    with sqlite3.connect(path) as db:
        return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def test_fresh_sqlite_gets_all_tables_and_columns(tmp_path):
    p = str(tmp_path / "a.db")
    SqliteSessionStore(p).close()
    AlertStore(p).close()
    Outbox(p).close()
    with sqlite3.connect(p) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "turns", "outbox", "alerts", "alert_events", "session_meta", "staff_access_log", "_yoyo_log", "_yoyo_migration"} <= names
    assert {"updated_at"} <= set(_cols(p, "sessions")) and {"rule_id", "resolved_by", "manual_notice_reported_at"} <= set(_cols(p, "alerts"))


def test_existing_pre_yoyo_sqlite_schema_is_accepted_and_data_kept(tmp_path):
    p = str(tmp_path / "old.db")
    with sqlite3.connect(p) as db:
        db.executescript(LEGACY_SESSIONS)
    store = SqliteSessionStore(p)                                   # 이미 있는 표 위에서 시작
    try:
        assert store.load("S1")["status"] == "OK"                   # 데이터가 그대로
    finally:
        store.close()


def test_pre_yoyo_sqlite_without_updated_at_gets_the_column(tmp_path):
    p = str(tmp_path / "older.db")
    with sqlite3.connect(p) as db:
        db.executescript(LEGACY_SESSIONS.replace(", updated_at REAL NOT NULL DEFAULT 0", "").replace("'{}', 5.0", "'{}'"))
    SqliteSessionStore(p).close()
    assert "updated_at" in _cols(p, "sessions")


def test_migrate_twice_is_a_noop(tmp_path):
    p = str(tmp_path / "t.db")
    for _ in range(2):
        migrate("sqlite", p)
    with sqlite3.connect(p) as db:
        assert db.execute("SELECT COUNT(*) FROM _yoyo_migration").fetchone()[0] == 5        # 5개 각각 한 번씩


def test_migration_error_does_not_leak_the_target():
    from survey_service.errors import StoreError
    with pytest.raises(StoreError) as e:
        migrate("postgresql", "postgresql://user:SECRETPW@127.0.0.1:1/db")
    assert "SECRETPW" not in str(e.value) and "user" not in str(e.value)


def test_postgres_and_sqlite_migrations_define_the_same_columns(request, tmp_path):
    from .pg_support import pg_url
    import psycopg
    url = pg_url(request)
    migrate("postgresql", url)
    migrate("postgresql", url)                                        # 두 번째는 아무 일도 하지 않는다
    p = str(tmp_path / "s.db")
    migrate("sqlite", p)
    with sqlite3.connect(p) as db:
        lite = {t: {r[1] for r in db.execute(f"PRAGMA table_info({t})")} for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%yoyo%' AND name NOT LIKE 'sqlite_%'")}
    with psycopg.connect(url) as c:
        pgt = {}
        for t, col in c.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()"):
            pgt.setdefault(t, set()).add(col)
    pgt = {t: v for t, v in pgt.items() if "yoyo" not in t}
    assert pgt == lite


def test_pre_yoyo_postgres_schema_is_accepted(request):
    from .pg_support import pg_url
    import psycopg
    url = pg_url(request)
    with psycopg.connect(url, autocommit=True) as c:                  # yoyo 도입 전 코드가 만든 모양(알림 표에서 나중에 더한 열이 없는 옛 모양)
        c.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, release_id TEXT NOT NULL, status TEXT NOT NULL, doc TEXT NOT NULL)")
        c.execute("INSERT INTO sessions VALUES ('S1', 1, 'rel', 'IN_PROGRESS', '{}')")
    migrate("postgresql", url)
    with psycopg.connect(url) as c:
        assert c.execute("SELECT updated_at, doc FROM sessions WHERE session_id='S1'").fetchone() == (0, "{}")
