# CR-017 칸 값 타입 필드, 위험 정지 DELIVERED 신호의 의미 명시

상태: **헤드 확인 요청(제안).** 서비스는 계약을 바꾸지 않는 범위로 구현했다.

## 1. 위험 정지 성공 신호 (횡단 H-2)

- 현재 계약(PUBLIC_API.md): `FrontendInput.turn_failure_signal`의 `staff_alert`가 `DELIVERED`이면 직원 알림이 전달됐다. 키오스크는 `DELIVERED`를 보고 대기 화면으로 간다.
- 서비스가 고친 것: 위험 정지로 알림이 전달된 턴에 `{write_committed: COMMITTED|NOT_COMMITTED, staff_alert: DELIVERED}`를 응답한다(이전에는 null이라 키오스크가 대기 화면으로 가지 않았다). 내부 로그·Trace의 실패 신호는 그대로다(실패가 아니다).
- 부족한 것: PUBLIC_API.md 34행은 "`turn_failure_signal`이 null이 아니면 이번 턴 처리에 문제가 있었다"고 적어 `DELIVERED`가 정상 위험 정지를 뜻한다는 점과 어긋난다. 제안: 34행에 "위험 정지로 직원 알림이 전달된 턴은 `staff_alert=DELIVERED`와 함께 non-null이다(문제 신호가 아니라 정지 알림)"를 추가하거나, 더 명확한 하위 호환 필드 `FrontendInput.session_state`(`ACTIVE`/`PAUSED_RISK`/`ENDED`, 선택)를 추가한다.

## 2. 칸 값 타입 (감사 TE-1)

- `QuestionBank`의 `SlotDef`에 값 타입이 없어 서비스는 처음 쓰는 자유 칸의 값 타입(불리언/정수/문자열)을 검증하지 못한다(`choices`, `yes_no_map`, `units`가 아는 만큼만 본다).
- 제안: `SlotDef.value_type`(`enum`: `boolean|integer|number|string|amount|choice`, 선택 필드, 없으면 현재처럼 추측)을 추가한다. 하위 호환(선택 필드). 반영되면 `extraction.check_values`가 추측 대신 이 필드를 쓴다.
