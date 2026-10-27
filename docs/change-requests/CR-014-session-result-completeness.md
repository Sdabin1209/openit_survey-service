# CR-014 결과 조회(SessionResult)의 종료 상태 정의와 완료 여부 표시

상태: **헤드 확인 요청.** 서비스 동작은 바꾸지 않았다(docs/boundary.md에 설명을 적었다). 스키마(`SessionResult`)와 PUBLIC_API.md B절은 소유가 달라 변경하지 않았다.

## 확인한 것

- 종료 상태는 이렇게 정해진다(코드: `extraction.plan_writes`, `graph.py`의 처리 표): `ENDED_COMPLETE` = 발화 턴에서 A가 `next_item=END`(마지막 문항까지 끝). `ENDED_LEFT` = 키오스크 `POST /v1/sessions/{id}/end` 또는 유휴 만료(이탈). `ENDED_STAFF` = 직원 개입 END. `ENDED_USER_STOP` = 스키마에만 있고 지금 만들어지지 않음.
- E2E에서 발화 한 턴 뒤 `/end`로 닫은 세션이 `ENDED_LEFT`인 것은 이 정의대로다.
- PUBLIC_API.md B절은 상태 이름만 나열하고 뜻을 적지 않았고, `SessionResult` 스키마의 `status`도 설명이 없다. 응답에는 완료 여부 필드가 없어 병원이 `status`를 직접 해석해야 한다. `answers`는 저장된 값만이고 `skipped`는 건너뛴 문항 ID라, 미응답 문항은 응답에서 알 수 없다.

## 제안 (선택, 계약 변경이라 헤드 결정)

1. PUBLIC_API.md B절에 추가할 문구: "`status` 뜻: `ENDED_COMPLETE` 문진을 끝까지 마침(완료는 이것뿐). `ENDED_LEFT` 환자가 끝나기 전에 떠남(키오스크 종료 요청 또는 유휴 만료), `ENDED_STAFF` 직원이 종료, `ENDED_USER_STOP` 예약(현재 만들어지지 않음), `IN_PROGRESS`·`PAUSED_RISK` 아직 끝나지 않음. 완료가 아닌 상태의 `answers`는 그때까지 저장된 부분 결과이며 `skipped`는 미응답 문항 목록이 아니다."
2. (선택) `SessionResult`에 `complete: boolean`(= status가 ENDED_COMPLETE)과 `answered_questions`/`total_questions` 같은 진행 정보를 더하면 오해 여지가 줄어든다. 필수 필드 추가라 소비자(병원 시스템)와 함께 정해야 한다.
3. `ENDED_USER_STOP`을 쓰지 않을 거면 스키마에서 빼거나 의미를 정해 달라(환자가 '그만할래요'라고 한 경우를 `ENDED_LEFT`와 구분하려는 것으로 짐작하나 확인하지 못했다).
