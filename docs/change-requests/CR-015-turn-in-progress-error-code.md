# CR-015 처리 중인 같은 turn_id의 오류 표기

상태: **처리됨 (헤드 결정, 2026-10-28).**

## 결정

계약 변경 없이 기존 규약(HTTP.md: 409의 사유는 `BoundaryError.details.reason`)을 따른다. 처리 중인 같은 turn_id의 409는 `code: "CONFLICT"`, `details.reason: "TURN_IN_PROGRESS"`, `Retry-After` 헤더 동봉이고 `retryable`은 CONFLICT 기본값을 유지한다. 의미는 boundary-contracts PUBLIC_API.md 409 행에 기재됐다. 전용 `code`는 만들지 않는다.

## 구현

- `survey_service/api.py`의 `_in_progress`가 이 형식으로 답한다. 이전의 message 접두사 방식은 폐기했다.
- 입력 불일치 409(같은 turn_id에 다른 내용)는 `details` 없음, `Retry-After` 없음으로 구분된다.
- 시험: `tests/test_turn_idempotency_deadline.py`(처리 중 409 형식, 입력 불일치 409와의 구분).

## 원래 제안(참고, 채택하지 않음)

`code: "TURN_IN_PROGRESS"`를 오류 코드에 추가하는 안.
