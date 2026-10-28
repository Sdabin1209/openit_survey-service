-- yoyo 도입 전 코드가 나중에 더한 열을 옛 데이터베이스에 채운다(새 데이터베이스는 0001에 이미 있어 아무 일도 하지 않는다, ADR-0030)
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS rule_id TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS resolved_by TEXT;
ALTER TABLE staff_access_log ADD COLUMN IF NOT EXISTS detail TEXT;
ALTER TABLE staff_access_log ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE session_meta ADD COLUMN IF NOT EXISTS device_id TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS turn_id TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS delivered TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS pause_reason TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS delivery_attempts TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_delivery_status TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_retry_at TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_retry_turn_id TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS manual_notice_outcome TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS manual_notice_reported_at TEXT;
