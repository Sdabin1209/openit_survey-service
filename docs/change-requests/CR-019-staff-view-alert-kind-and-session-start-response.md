# CR-019: 직원 세션 보기의 알림 kind 이름과 세션 시작 응답의 session_id (단계 B, 2026-10-29)

상태: **스키마 변경 필요, 변경하지 않았다**(지시: 스키마 변경이 필요하면 변경 요청으로 보고). 대상은 이 영역의 문진 스키마 `schema/schemas/`이므로 헤드가 승인하면 survey-service가 반영한다(D21).

## 1. StaffSessionView.alerts[].kind
- 계약(`PUBLIC_API.md` 48행, 2.2.0): 목록 `StaffAlert`와 세션 보기 `StaffSessionView.alerts[]`는 같은 kind 이름(`RISK_STOP`, `CALL_STAFF`, `USER_CALLED_STAFF`)을 쓴다.
- 현재 코드(`api.py` `_staff_session`): `KIND_MAP`을 역변환해 `RISK_STOP`을 `RISK`로 내보낸다.
- 막힘: `schema/schemas/StaffSessionView.json`의 `$defs/AlertKind`가 `RISK`, `CALL_STAFF`, `USER_CALLED_STAFF`만 허용한다. 역변환을 없애면 이 영역 스키마 위반이 된다(`tests/test_staff_view_alert_kind_names.py`의 둘째 시험이 증명).
- 요청: `StaffSessionView`의 `AlertKind`를 `RISK_STOP`, `CALL_STAFF`, `USER_CALLED_STAFF`로 바꾼다(정수 스키마 버전 영향은 헤드가 판단: 값 이름 변경은 추가형이 아니다). 같은 `AlertKind` 정의가 `TurnTrace`, `StaffConsoleInput`, `OrchestratorOutput`, `SessionStore`에도 `RISK`로 들어 있어 이들과의 관계(내부 알림 종류 `RISK`와 공개 이름 `RISK_STOP`)를 헤드가 정해야 한다. 이 영역의 제안: 공개 두 스키마(`StaffSessionView`와 목록 쪽)만 `RISK_STOP`으로 하고 내부 `AlertKind`는 그대로 둔다.
- 반영 뒤 할 일: `api.py`의 역변환 두 줄을 지우고 `tests/test_staff_view_alert_kind_names.py`의 xfail 표식을 지운다(strict라 스키마·코드가 맞으면 XPASS로 알려 준다).

## 2. POST /v1/sessions 응답의 session_id
- 계약(`PUBLIC_API.md` 7행): `session_id`를 생략하면 서버가 발급한 값을 응답의 `session_id`로 돌려준다.
- 현재 응답은 `FrontendInput`(`schema/schemas/FrontendInput.json`, `additionalProperties: false`, 필드 `response_text`·`turn_failure_signal`)이다. 구현은 서버가 발급했을 때만 응답에 `session_id`를 더한다. 이 응답은 스키마상 `FrontendInput`이 아니다.
- 요청: `FrontendInput`에 optional `session_id`를 더하거나, 세션 시작 전용 응답 스키마를 둔다. 전자가 간단하나 모든 턴 응답의 모양이 넓어진다. 어느 쪽이든 헤드가 계약 `PUBLIC_API.md`의 응답 표(24행)를 함께 고쳐야 한다.
- 임시 처리: 프론트가 정한 ID는 응답에 되돌리지 않아 2.x 모양이 그대로다.

## 3. 알려진 한계(요청은 아니다)
서버 발급 방식에서 시작 요청이 시간 초과로 응답을 잃으면 프론트는 ID를 모르므로 같은 요청을 다시 보내도 새 세션이 생긴다(고아 세션은 유휴 만료가 닫는다). 계약에 재시도 규칙이 없다. 필요하면 헤드가 `turn_id`를 시작 요청의 멱등 키로 쓰는 규칙을 정한다(현재 구현은 session_id 없이 온 시작 요청의 turn_id를 세션 간 멱등 키로 쓰지 않는다).
