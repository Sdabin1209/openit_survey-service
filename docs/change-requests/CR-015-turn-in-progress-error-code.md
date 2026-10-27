# CR-015 처리 중인 같은 turn_id의 오류 표기

상태: **헤드 확인 요청(선택).** 서비스는 계약을 바꾸지 않는 범위로 구현했다.

## 구현한 것 (FAULT_POLICY 원칙 2)

- 같은 (세션, turn_id)가 처리 중일 때 들어온 같은 turn_id 요청은 새로 시작하지 않고 `409`와 `Retry-After`로 답한다. 본문은 기존 오류 형식을 재사용한다: `code: "CONFLICT"`, `message`가 `TURN_IN_PROGRESS:`로 시작, `retryable: false`.
- 이미 확정된 turn_id는 저장된 결과를 그대로 돌려준다(전부터 그랬다).

## 부족한 것

- PUBLIC_API.md 오류 코드 열거에 `TURN_IN_PROGRESS`가 없어 클라이언트는 메시지 접두사로 구분해야 한다. `CONFLICT`는 "같은 turn_id에 다른 입력"(재시도해도 같음)과 "처리 중"(재시도하면 됨)이 섞인다.

## 제안

- `code: "TURN_IN_PROGRESS"`(HTTP 409, `retryable: true`)를 오류 코드에 추가한다. 승인되면 `HTTP_CODE`에 한 줄과 `_in_progress`의 코드만 바꾸면 된다. 그때까지 키오스크는 409 + `Retry-After`면 같은 turn_id로 재시도하고, 메시지가 `TURN_IN_PROGRESS:`가 아닌 409(입력 불일치)는 재시도하지 않는 규칙을 쓸 수 있다.
