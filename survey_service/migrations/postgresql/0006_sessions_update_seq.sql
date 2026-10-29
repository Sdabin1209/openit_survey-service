-- 결과 목록의 순서·커서를 위한 단조 증가 번호(U26, ADR-0034). 쓸 때마다 시퀀스에서 nextval을 받는다(여러 연결이 동시에 써도 번호가 겹치지 않는다).
-- 옛 행은 옛 순서(updated_at, session_id) 그대로 번호를 받는다.
CREATE SEQUENCE IF NOT EXISTS sessions_update_seq;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS update_seq BIGINT NOT NULL DEFAULT 0;
UPDATE sessions SET update_seq = s.rn FROM (SELECT session_id, row_number() OVER (ORDER BY updated_at, session_id) AS rn FROM sessions) s WHERE sessions.session_id = s.session_id;
SELECT setval('sessions_update_seq', COALESCE((SELECT MAX(update_seq) FROM sessions), 0) + 1, false);
CREATE INDEX IF NOT EXISTS sessions_update_seq_idx ON sessions(update_seq);
