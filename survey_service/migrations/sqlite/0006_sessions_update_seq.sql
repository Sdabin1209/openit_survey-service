-- 결과 목록의 순서·커서를 위한 단조 증가 번호(U26, ADR-0034). 쓸 때마다 저장소가 (최댓값 + 1)을 매긴다(SQLite는 쓰기가 한 번에 하나라 겹치지 않는다).
-- 옛 행은 옛 순서(updated_at, session_id) 그대로 번호를 받는다.
ALTER TABLE sessions ADD COLUMN update_seq INTEGER NOT NULL DEFAULT 0;
UPDATE sessions SET update_seq = (SELECT COUNT(*) FROM sessions s2 WHERE (s2.updated_at, s2.session_id) <= (sessions.updated_at, sessions.session_id));
CREATE INDEX IF NOT EXISTS sessions_update_seq ON sessions(update_seq);
