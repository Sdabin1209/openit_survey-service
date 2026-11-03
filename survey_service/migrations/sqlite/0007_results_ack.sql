-- 결과 확인 표시(U83, ADR-0056): 의사가 결과를 확인한 계정과 시각. 세션 메타의 선택 열. 두 번째 확인은 처음 값을 바꾸지 않는다.
ALTER TABLE session_meta ADD COLUMN acknowledged_by TEXT;
ALTER TABLE session_meta ADD COLUMN acknowledged_at TEXT;
