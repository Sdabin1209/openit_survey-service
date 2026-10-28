"""yoyo 도입 전 코드가 나중에 더한 sessions.updated_at을 옛 SQLite 파일에 채운다(새 파일은 0001에 이미 있다, ADR-0030).
왜 파이썬인가: SQLite의 ALTER TABLE ADD COLUMN에는 IF NOT EXISTS가 없다."""
from yoyo import step


def add_updated_at(conn):
    cur = conn.cursor()
    if "updated_at" not in [r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()]:
        cur.execute("ALTER TABLE sessions ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
    cur.execute("CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(status, updated_at)")


step(add_updated_at)
