# CR-013 직원 세션 조회 API (`GET /v1/staff/sessions/{session_id}`)를 PUBLIC_API.md에 추가

상태: **헤드가 PUBLIC_API.md에 반영해 주세요.** 이 영역은 구현을 끝냈다(`survey_service/api.py`, 스키마 `schema/schemas/StaffSessionView.json`). `boundary-contracts`는 헤드 소유라 직접 고치지 않았다.

문진 스키마 정수 버전(`CONTRACT_VERSION`)은 올리지 않았다(이유는 CHANGELOG). 응답 모양은 프론트와 합의된 것을 그대로 따랐고, 아래 "합의된 모양과 다른 점"만 확인이 필요하다.

## PUBLIC_API.md '직원 콘솔' 절에 추가할 문구 (그대로 붙여 넣을 것)

> ### 세션 기록 조회
>
> `GET /v1/staff/sessions/{session_id}` (역할 staff)
>
> 직원이 한 세션의 대화 기록과 저장된 값, 그 세션의 알림을 읽는다. 읽기 전용이며 세션과 알림을 바꾸지 않는다.
>
> - 인증: 다른 `/v1/staff/*`와 같다. `Authorization: Bearer <직원 토큰>`. 토큰이 없거나 틀리면 401, 키오스크 토큰이면 403(`PERMISSION_DENIED`).
> - 경로의 `session_id`는 영문·숫자·`-`·`_` 1~64자. 형식이 틀리면 400(`INVALID_RECORD`), 세션이 없으면 404(`NOT_FOUND`), 세션 저장소·알림 저장소를 읽을 수 없으면 503(`UNAVAILABLE`, `Retry-After`).
> - 응답 200: `StaffSessionView`(문진 스키마 `StaffSessionView.json`).
>
> ```json
> {"session_id": "K1", "status": "IN_PROGRESS", "release_id": "rel-1", "created_at": "2026-10-27T08:00:00Z",
>  "turns": [{"turn_index": 0, "turn_id": "start", "at": "2026-10-27T08:00:00Z", "utterance": null, "response_text": "안녕하세요.", "action": null}],
>  "values": [{"key": "Q1.D01.diagnosed", "value": false, "status": "ANSWERED", "precision": "exact"}],
>  "alerts": [{"alert_id": "al-1", "kind": "RISK", "status": "OPEN", "created_at": "2026-10-27T08:01:00Z", "rule_id": "rule-1"}]}
> ```
>
> - `status`는 세션 상태(`IN_PROGRESS`, `PAUSED_RISK`, `ENDED_COMPLETE`, `ENDED_STAFF`, `ENDED_LEFT`, `ENDED_USER_STOP`). `release_id`는 세션이 묶인 릴리스.
> - `turns`는 대화 로그 순서(`turn_index` 0부터). `utterance`는 환자 발화 원문(시작·종료·직원 개입 항목은 null), `response_text`는 환자에게 나간 문장(없으면 null), `action`은 LoRA-A가 낸 절차(NEXT, CORRECT, REASK, ...)이고 A를 부르지 않은 항목은 null.
> - `values`는 값이 저장된 칸만. `key`는 모델 키(`Q7_1[soju].amount`), `status`는 `ANSWERED`(정정된 적이 있으면 `CORRECTION`), `precision`은 `exact|approximate|unspecified`. `value`는 임의 JSON 값.
> - `alerts`의 `kind`는 `RISK|CALL_STAFF|USER_CALLED_STAFF`, `status`는 `OPEN|RESOLVED`. `rule_id`는 위험 규칙 엔진이 낸 알림에만 있고 나머지는 null. 알림에 발화 원문은 없다.
> - `created_at`, `turns[].at`은 서버가 그 기록을 남긴 UTC 시각(`YYYY-MM-DDTHH:MM:SSZ`). 시각을 남기기 시작하기 전에 만든 세션의 항목은 `turn_id`·`at`·`created_at`이 null이다.
> - **접근 기록**: 이 호출은 환자 데이터를 내보내므로 서버가 호출마다 (시각, 토큰의 역할 이름, session_id, 결과)를 남긴다. 토큰 값과 발화 내용은 남기지 않는다. 기록을 남기지 못하면 데이터를 주지 않고 503을 돌려준다. 거부된 호출(401·403)은 데이터를 내지 않으므로 이 기록에 넣지 않는다.

## 합의된 모양과 다른 점 (확인 요청)

1. **`created_at`, `turns[].turn_id`, `turns[].at`이 null일 수 있다.** 합의된 모양은 문자열이다. 세션 문서(`SessionStore`)에는 시각도 turn_id도 없어서(대화 로그 항목에 필드가 없다) 이 영역이 알림 DB의 새 테이블 `session_timeline`에 턴마다 남기기 시작했다. 그 이전에 만든 세션은 값이 없어 null이다. 새 세션은 항상 값이 있다. 프런트는 null을 견뎌야 한다. 문서 모양(SessionStore)에 시각을 넣는 쪽이 더 바르지만 정수 버전과 저장소 이전(PostgreSQL)에 걸려 이번에는 하지 않았다.
2. **`values[].status`의 값 집합이 합의되지 않았다.** LoRA-A의 `values[].status`와 같은 이름을 썼고 저장된 값은 `ANSWERED` 또는 `CORRECTION`뿐이다(제안, 프런트 확인 필요).
3. 직원 알림 목록(`GET /v1/staff/alerts`)과 SSE의 알림 모양에는 `rule_id`를 넣지 않았다(공개된 모양을 바꾸지 않으려고). 필요하면 별도 요청.
