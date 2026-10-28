"""yoyo 도입 전 코드가 나중에 더한 열을 옛 SQLite 파일에 채운다(새 파일은 0001에 이미 있어 아무 일도 하지 않는다, ADR-0030).
왜 파이썬인가: SQLite의 ALTER TABLE ADD COLUMN에는 IF NOT EXISTS가 없다."""
from yoyo import step

COLUMNS = (('alerts', 'rule_id'), ('alerts', 'resolved_by'), ('staff_access_log', 'detail'), ('staff_access_log', 'user_id'), ('session_meta', 'device_id'), ('alerts', 'turn_id'), ('alerts', 'delivered'), ('alerts', 'pause_reason'), ('alerts', 'delivery_attempts'), ('alerts', 'last_delivery_status'), ('alerts', 'last_retry_at'), ('alerts', 'last_retry_turn_id'), ('alerts', 'manual_notice_outcome'), ('alerts', 'manual_notice_reported_at'))


def add_missing_columns(conn):
    cur = conn.cursor()
    for table, col in COLUMNS:
        if col not in [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")


step(add_missing_columns)
