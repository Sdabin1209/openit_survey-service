# 4.1.4

- FrontendMessage의 SESSION_START.session_id 설명과 모든 내장 사본을 바로잡는다. 공개 시작 본문에는 금지하고, 서버 발급 뒤 내부 메시지와 Trace에 넣는다. 스키마 모양·정수 계약 버전은 바뀌지 않는다.

# 4.1.3

- 세션 시작 응답 설명에서 클라이언트가 ID를 발급하던 폐기된 경우를 제거한다.
- TurnFailureSignal.session_id와 모든 내장 사본을 공개 세션 ID 형식(영문·숫자·`_`·`-`, 1~64자, 끝 개행 거부)에 맞춘다. 이전 minLength 전용 정의가 허용하던 형식은 거부한다. 실제 서버 발급 형식과 모델 입출력은 불변이며 정수 계약 버전 1을 유지한다.

# 4.1.2

- 문자열 정규식의 `$` 뒤에 `(?![\s\S])`를 붙여 마지막 개행 앞에서 일치하는 검증기 차이를 없앤다. 정수 계약 버전과 정상 입력 형식은 같다.

# 문진 스키마 변경 이력

이 디렉터리(`schema/`)는 survey-service 영역이 소유한다(D29). 패키지 버전(`pyproject.toml`)은 배포 이름 `openit-survey-schema`의 SemVer이고, 아래 "정수 버전"과 다르다.

## 정수 버전 규칙 (계약 CONVENTIONS 12절, G11)

`schema/CONTRACT_VERSION`(정수, 현재 1)은 릴리스의 `runtime_contract_version`이고 문항 은행·칸 키를 학습한 모델 패키지의 호환 번호다. TurnTrace를 쓰는 survey-service와 읽는 ops-feedback은 같은 정수를 설치해야 한다.

- **올려야 하는 변경**(읽는 쪽이 깨지는 변경): 키 삭제, 이름 변경, enum 축소, 필수 추가.
- **같은 정수 안에서 허용하는 변경**: 추가만(선택 필드, enum 값 추가, 설명).
- 올릴 때는 반영 전에 헤드에 알린다(D21). 올리면 survey-service와 ops-feedback이 같은 정수를 함께 배포한다.
- 주의: enum 값 추가는 "추가"이지만 읽는 쪽이 enum을 닫힌 집합으로 검증하면 새 값을 거부한다. 같은 정수 안의 추가라도 읽는 쪽 영역에 알린다.

## 정수 버전 1 안의 비호환 변경 이력 (G11)

정수 버전을 올리지 않고 들어간 변경 중 읽는 쪽에 영향을 줄 수 있는 것. 이 목록은 이 영역이 알고 있는 것이며, 읽는 쪽이 실제로 깨졌는지는 확인하지 못했다.

| 패키지 버전 | 변경 | 읽는 쪽 영향 | 근거 |
| --- | --- | --- | --- |
| 2.0.0 | `SessionResult.skipped`(문자열 목록) 폐지, `skipped_details`만 남김 | 결과를 읽는 병원 시스템이 `skipped_details`로 옮겨야 한다(키 삭제 = 규칙상 깨지는 변경). 모델이 보지 않는 출력 형식이라 정수를 올리지 않았다 | docs/adr/ADR-0044.md |
| 2.1.0 | `TurnFailureSignal`·`SessionState`·`StaffSessionView`·`TurnTrace`에 `pause_reason`(선택) 추가 | 추가만. 영향 없음 | CR-H002, docs/adr/ADR-0022.md |
| 3.0.0 | 계약 3.0.0 전환(`ActionExtraction` 등)에 맞춘 개정 | 위 2.0.0과 같은 묶음 | docs/adr/ADR-0044.md |
| 4.0.0 | 문진표(문항 세트) 여러 개 동시 활성(사용자 결정 2026-11-03): `QuestionBank`에 필수 `question_set_id`·`title` 추가, `Prerequisite`를 `{slot, op, value}`로 바꿈(op 6개), `SessionResult`·`StaffSessionView`에 필수 `question_set_id`·`question_set_title`(null 가능) 추가, `FrontendMessage` SESSION_START에 선택 `question_set_id`, `QuestionFlow` 스키마와 `data/question_flow.json` 삭제(흐름은 문진표에서 파생) | 정수 1 유지: 모델 입출력 불변(계약 CONVENTIONS 12절 2026-11-03 개정). 문진표 ID 없는 옛 문항 은행은 QuestionBank 검증에서 거부된다 | docs/adr/ADR-0057.md |

## 같은 정수 버전 1 안의 추가

| 날짜 | 변경 | 읽는 쪽 영향 |
| --- | --- | --- |
| 2026-11-08 | 설명 문구만 바꿈(구조·필드·제약 변경 없음): `subject_ref`·`SessionResult` 설명의 "병원"을 "설치처"로(`FrontendMessage`·`OrchestratorInput`·`StaffSessionView`·`TurnTrace`·`SessionResult`) | 읽는 쪽 영향 없음. 아래 과거 행은 당시 기록이라 "병원" 표현을 그대로 둔다 |
| 2026-11-03 | 패키지 4.1.1(PATCH, 정본은 그대로): `TurnTrace.json`·`OrchestratorInput.json` 안의 `$defs/FrontendMessage`(와 TurnTrace의 `FrontendInput`) 사본이 4.0.0 변경(SESSION_START의 `question_set_id`·`subject_ref`)을 따르지 않아 세션 시작 Trace 봉투 검증이 SchemaViolation(503)이었다. 사본을 정본(`FrontendMessage.json`, `FrontendInput.json`)과 같게 고쳤고, 사본이 어긋나면 실패하는 정적 시험을 더했다 | 정본 변경 없음. 사본이 넓어졌을 뿐이라 영향 없음(SESSION_START에 선택 필드가 허용된다) |
| 2026-11-03 | `SessionResult`에 선택 필드 `pause_reason`(RISK·REASK_LIMIT·OTHER, 상태가 PAUSED_RISK인 결과에만, 패키지 4.1.0, L28) | 선택 필드 추가라 영향 없음. 의사는 부분 결과의 정지 사유를 본다 |
| 2026-11-03 | `SessionResult.skipped_details[].reason`에 `REASK_LIMIT` 추가(되묻기 한도로 빈칸). `SessionStore`·`TurnTrace`·`DispatchInput`의 `SkipReason`에 `REASK_LIMIT` 추가(진행 기록의 건너뛴 문항 사유) | enum 값 추가. 닫힌 enum으로 검증하는 읽는 쪽(ops-feedback의 TurnTrace 읽기, 병원 시스템의 결과 읽기)은 새 값을 받아들여야 한다. 헤드가 해당 영역에 알린다. 근거 docs/adr/ADR-0054.md |
| 2026-11-03 | `SessionResult`에 선택 필드 `acknowledged_by`(문자열), `acknowledged_at`(UTC 시각) 추가(결과 확인 표시, U83). 확인 전에는 없다 | 선택 필드 추가라 영향 없음. 근거 docs/adr/ADR-0056.md |

위 표의 2.0.0·2.1.0·3.0.0 행의 날짜와 세부는 `docs/adr/`와 `docs/change-requests/`가 정본이다. 이 표는 그 요약이며, 읽지 못한 이력이 있으면 빠졌을 수 있다(추정).
