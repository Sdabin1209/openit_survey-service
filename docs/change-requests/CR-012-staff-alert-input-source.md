# CR-012 직원 알림의 입력원 (위험 신호·직원 호출) 과 반복 물음 상한

상태: **미정, 헤드 결정 필요.** 구현은 알림 경로를 그대로 두고 입력원이 없으면 비활성으로 두었다(`/readyz`의 `staff_alert_source`).

## 확인한 것

v1에서 직원 알림(`StaffAlert`)과 세션 정지(`PAUSED_RISK`)는 아래에서 왔다(turn-engine 0.3.0의 폐기 표시된 `planner.py`와 v1 서비스 코드에서 읽음).

| v1 입력원 | 읽는 곳 | 결과 |
|---|---|---|
| 파서 항목의 위험 신호 의도(`RISK_SIGNAL`) -> 판정기의 `utterance_judgment.risk_signal`. 판정기는 문항 세트 `session_settings.risk_phrases`를 입력으로 받았다 | LoRA-A 출력 + 문항 세트 | `RISK`/`RISK_SIGNAL` 알림, `PAUSED_RISK` |
| 슬롯 `risk_rule`(IMMEDIATE)과 저장 값 | 문항 세트 + 판정 | `RISK`/`RISK_RULE` 알림, `PAUSED_RISK`. ON_RESULT는 `risk_mark` |
| 파서의 `STAFF_REQUEST` 요청 | LoRA-A 출력 | `USER_CALLED_STAFF` |
| 물음 상한(`ask_limit`)·연속 못 알아들음 상한(`unrecognized_streak_limit`) | Plan 내부 카운터 | `CALL_STAFF` |

v2의 A 출력(`ActionExtraction`)에는 위 신호가 없다: action은 NEXT·CORRECT·REASK·CONFIRM·HOLD·SKIP·REDIRECT, REDIRECT 사유는 OUT_OF_SCHEMA·OFF_TOPIC·ASK_PURPOSE, `note`는 OUT_OF_SCHEMA의 요약 문장이다. 정규화기·판정기·Plan 코드는 서비스에 없다(D74).

## 이 영역이 한 것

- 알림 저장·전달·SSE·웹 푸시·해결, 정지 세션의 직원 재개·종료 경로는 코드를 유지했다(시험 통과).
- 알림을 만드는 곳은 `Orchestrator(risk_source=...)`(Protocol `RiskSource`, 설정 `SURVEY_RISK_SOURCE=모듈:함수`) 하나다. 꽂지 않으면 알림이 만들어지지 않는다. 조용히 끄지 않고 `/readyz`에 `staff_alert_source: {active: false, reason: NO_INPUT_SOURCE_IN_V2_EXTRACTION}`로 낸다.
- 시험·스모크에는 시험용 대역(`ScriptedRisk`)만 쓴다. 운영 입력원은 지어내지 않았다.

## 결정해 주세요 (선택지, 이 영역의 권고 없음)

1. 위험·직원 호출을 어디서 잡을지: (a) A 출력에 필드를 더하도록 데이터셋 담당에 요청(계약 MINOR 이상), (b) 서버 쪽에서 위험을 따로 잡는 규칙·모델 층(model-runtime 영역), (c) 문항 세트의 `risk_rule`·`risk_phrases`를 서비스가 평가(D74(1) "서비스에 판단 코드를 두지 않는다"와 충돌), (d) 키오스크 도움 요청 같은 다른 경로.
2. 같은 문항에서 REASK·CONFIRM이 반복될 때 끊는 상한(`ask_limit` 등)과 `CALL_STAFF`: v2에서는 서비스에 상한이 없어 A가 되묻기를 계속 내면 끝나지 않는다.
3. 위험 정지 중 응답 문장: 지금은 문항 세트의 고정 문장(`RISK_NOTICE`, 재개 `RESUME`, 직원 종료 `SURVEY_STOPPED`)을 모델 없이 쓴다(제안). B에 이런 action이 없다.

## 다른 영역에 필요한 것

- 데이터셋 담당: 1(a)를 택하면 A 출력의 필드와 데이터.
- deploy/키오스크: 입력원이 정해질 때까지 운영에서 직원 알림이 나지 않는다는 점을 알아야 한다(`/readyz`로 확인 가능).
