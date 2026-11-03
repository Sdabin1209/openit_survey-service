-- 세션이 문진하는 문진표(ID·제목)를 세션 행에 둔다(ADR-0057). 세션을 만들 때 정해지고 바뀌지 않는다. 열이 없는 옛 세션은 NULL(문진표 없이 만든 세션).
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS question_set_id TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS question_set_title TEXT;
