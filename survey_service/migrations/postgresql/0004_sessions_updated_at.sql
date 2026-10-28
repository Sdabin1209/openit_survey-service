-- yoyo 도입 전 코드가 나중에 더한 열을 옛 데이터베이스에 채운다(새 데이터베이스는 0001에 이미 있다, ADR-0030)
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(status, updated_at);
