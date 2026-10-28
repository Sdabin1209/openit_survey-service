-- Trace outbox 표. 기존 파일·데이터베이스에서 시작해도 되도록 IF NOT EXISTS (yoyo 도입 전 코드가 만든 스키마를 그대로 받아들인다, ADR-0030)
CREATE TABLE IF NOT EXISTS outbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      TEXT NOT NULL,
  turn_id         TEXT NOT NULL,
  envelope        TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','SENT','DEAD')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  last_error      TEXT,
  created_at      REAL NOT NULL,
  finished_at     REAL,
  UNIQUE (session_id, turn_id)
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_attempt_at, id);
