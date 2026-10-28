-- 세션·턴 표. 기존 파일·데이터베이스에서 시작해도 되도록 IF NOT EXISTS (ADR-0030)
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  revision   INTEGER NOT NULL,
  release_id TEXT NOT NULL,
  status     TEXT NOT NULL,
  doc        TEXT NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_status ON sessions(status);
CREATE TABLE IF NOT EXISTS turns (
  session_id  TEXT NOT NULL,
  turn_id     TEXT NOT NULL,
  input_hash  TEXT NOT NULL,
  committed   INTEGER NOT NULL DEFAULT 0,
  result_json TEXT,
  PRIMARY KEY (session_id, turn_id)
);
