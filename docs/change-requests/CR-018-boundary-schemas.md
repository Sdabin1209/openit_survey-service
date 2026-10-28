# CR-018: 영역 경계를 넘는 형식의 스키마 초안 (지시서 1 B, 2026-10-28)

상태: **초안 제출**. 코드는 바꾸지 않았다. 헤드가 계약(boundary-contracts)에 올린 뒤에 이 영역이 쓰도록 바꾼다(다음 단계). 근거 점검: `docs/audit/structure-rules-2026-10-28.md` E절(SV-14, SV-15, SV-16).

초안은 `docs/change-requests/schemas/*.schema.json`(JSON Schema 2020-12), 예시는 `docs/change-requests/schemas/examples/`, 검증은 아래 명령이다.

```
.venv/bin/python docs/change-requests/schemas/verify_boundary_schemas.py
```

## 검증 출력 (이 PC, jsonschema `Draft202012Validator`, 실행: 위 명령)

```
OK   service-env.valid.json                           통과
OK   service-env.invalid.json                         거부 (required: )
OK   readyz-body.valid.json                           통과
OK   readyz-body.invalid.json                         거부 (maxItems: problems)
OK   purge-output.valid.json                          통과
OK   purge-output.valid-session.json                  통과
OK   purge-output.invalid.json                        거부 (const: event)
OK   image-labels.valid.json                          통과
OK   image-labels.invalid.json                        거부 (required: )
OK   question-flow-table-args.valid.json              통과
OK   question-flow-table-args.invalid.json            거부 (additionalProperties: skips/0)
OK   question-bank-read.valid-request.json            통과
OK   question-bank-read.valid-response.json           통과
OK   question-bank-read.invalid-response.json         거부 (pattern: content_hash)
OK   service-env: SURVEY_ 접두 오타 키 하나 -> 거부
OK   service-env: 비밀을 NAME과 NAME_FILE로 동시에 -> 거부
OK   service-env: http인데 URL 없음 -> 거부
OK   image-labels: io.openit.* 모르는 키 하나 -> 거부
OK   readyz-body: ready=false인데 problems 비어 있음 -> 거부
OK   question_bank는 문진 스키마 QuestionBank(정수 버전 1)도 통과
OK   content_hash == sha256_hash(question_bank) (boundary_contracts.canonical)
검증 14건, 어긋남 0건
```

"거부" 줄이 거부되어야 할 예시이고, `service-env`·`image-labels`·`readyz-body`는 규칙마다 유효 예시를 한 군데만 망가뜨려 그 규칙 때문에 거부되는지도 따로 확인했다. 예시는 생성 스크립트가 아니라 파일로 남겼다.

## 형식별 설명

### 1. 환경 변수와 비밀 파일 이름: `service-env.schema.json`
- 누가: deploy(compose)가 만들고 `survey_service/settings.py`(와 `db.py`, `clients.py`)가 읽는다. 이 영역은 **읽는** 쪽.
- 예시: `examples/service-env.valid.json`(runbook §1과 `scripts/smoke.sh` 구성에서 가져온 http 구성, 비밀은 `_FILE`).
- 모르는 키: `SURVEY_` 접두의 모르는 이름은 **거부**한다(현재 코드는 조용히 무시해서 오타가 배포 뒤에야 드러난다). 접두가 다른 키는 무시.
- 불변식: 필수 3개(`SURVEY_RELEASE_READER`, `SURVEY_TRACE_INGEST`, `SURVEY_MODEL_BACKEND`), `http`면 해당 `_URL` 필수, 비밀은 NAME과 NAME_FILE을 함께 주지 않고 `_FILE`은 절대 경로(D84). 값 자체는 스키마에도 예시에도 없다.
- 한계: 허용 이름 목록은 코드에서 `grep -rhoE "SURVEY_[A-Z0-9_]+" survey_service`로 뽑은 것이다. 새 설정이 생기면 이 스키마도 같이 바꿔야 한다(계약에 오르면 그 변경이 헤드를 거친다).

### 2. `/readyz` 본문: `readyz-body.schema.json`
- 누가: survey-service(`service.py` `readiness`)가 만들고 deploy(경보·점검, `docs/boundary.md` 경보 기준)가 읽는다. **내는** 쪽.
- 예시: `examples/readyz-body.valid.json`는 시험 장비(`tests/test_staff_alerts.py make_app`)로 띄운 실제 `App.readiness()` 출력이다.
- 모르는 키: 읽는 쪽이 **무시**(이 서비스가 진단 필드를 더해도 deploy가 깨지지 않게). 필수는 deploy가 읽는 것만(`problems`, `trace_outbox.dead_by_code/dropped_full`, `staff_alerts.delivery_failed`, `staff_alert_source.provisional`, `kiosk_tokens.shared_kiosk_token`, `release_loader.retrying`)이다.
- 불변식: `ready`와 `problems`는 일치(true면 비어 있고 false면 비어 있지 않다). 비밀 값은 어떤 키에도 없다(`secrets`는 file/env/unset만). 200/503과 마감 시간은 이미 CONVENTIONS가 정한다.
- 확인 못 한 것: boundary.md가 경보 입력으로 적은 `api.auth_failures`는 인증 실패가 한 번도 없을 때 본문에 없다(시험 장비 출력에 없었다). 스키마는 선택 키로 뒀다.

### 3. `purge` 명령 출력: `purge-output.schema.json`
- 누가: `python -m survey_service purge [--session <id>]`(`retention.py` `run_purge`)가 표준 출력에 한 줄 JSON을 낸다. deploy 잡이 읽는다. **내는** 쪽.
- 예시: `purge-output.valid.json`(전체 정리), `purge-output.valid-session.json`(`--session`). 둘 다 실제 실행 출력이다(빈 DB).
- 모르는 키: 무시. 종료 코드 0/2/3과 실패 때 표준 오류 한 줄은 스키마 설명에 적었다(출력 JSON이 아니다).
- 불변식: `event`는 항상 `session_purge`, 건수는 0 이상, 보존 기간은 무기한 없음(`retention_days` ≥ 1, ADR-0015).

### 4. 추가 이미지 라벨: `image-labels.schema.json`
- 누가: survey-service `Dockerfile`이 만들고 deploy가 읽는다. **내는** 쪽. 공통 6개(D50)에 `io.openit.langgraph`(CR-011, D63)를 더한 7개.
- 예시: `image-labels.valid.json`은 `scripts/smoke.sh`가 이번 빌드 이미지에서 읽어 출력한 라벨이다.
- 모르는 키: `io.openit.*`의 모르는 키는 **거부**(오타 방지), 다른 접두는 무시.
- 불변식: `io.openit.service`=`survey-service`, 버전 값은 `vX.Y.Z` 형식(langgraph는 숫자만), `survey-schema`는 정수 문자열.

### 5. 문항 흐름 표를 turn-engine에 넘기는 형식: `question-flow-table-args.schema.json`
- 지시서의 "추론 요청에 싣는 QuestionFlow 형식"에 대해: 추론 서버(I9) 요청에는 QuestionFlow가 실리지 않는다(`extraction.py`는 표에서 계산한 `ActionExtractionInput.state`만 보낸다, 그 형식은 이미 계약의 `ActionExtractionInput`이다). 계약에 없는 것은 **문진 스키마 `QuestionFlow` → turn-engine `QuestionFlowTable`(I8)** 로 넘기는 값이어서 그것을 스키마로 썼다. 해석이 다르면 알려 달라.
- 누가: survey-service(`flow.py` `build_flow`)가 `QuestionFlow`에서 변환해 만들고 `turn_engine.QuestionFlowTable(order, skips=[SkipRule(slot, value, skip_item)])`이 읽는다. **내는** 쪽.
- 예시: `question-flow-table-args.valid.json`는 `schema/data/question_flow.json`(`dataset_v2`)에서 변환한 실제 값이다.
- 모르는 키: **거부**(생성자 인자가 바뀌면 바로 드러나게). 거부 예시는 변환 전의 `op` 키가 남은 경우다.
- 불변식: `order`는 비지 않고 중복이 없다. `skips[].skip_item`과 `slot`의 문항이 `order`에 있다는 것은 JSON Schema로 표현할 수 없어 문진 스키마 시험이 맡는다(스키마의 `$comment`).

### 6. 번들에서 문항 세트만 읽는 호출: `question-bank-read.schema.json`
- 필요: SV-16. 문항 세트를 얻는 유일한 호출이 `load_bundle`이라 어댑터까지 내려받는다. I4에 `get_release_question_bank(release_id)`를 더하자는 제안이다(이름은 제안).
- 누가: release-gate(서버)가 만들고 survey-service(클라이언트)가 읽는다. 이 영역은 **받는** 쪽. 스키마는 `request`와 `response`를 `properties`에 나눠 담았다(하나씩 검증).
- 예시: 응답의 `question_bank`는 `tests/sample_bank.py`의 실제 문항 세트이고 문진 스키마 `QuestionBank`도 통과한다. `content_hash`는 `boundary_contracts.canonical.sha256_hash`로 계산한 값이다(검증 스크립트가 다시 계산해 대조).
- 모르는 키: 응답은 읽기 모드로 **받아들인다**(CONVENTIONS 1절), 요청은 엄격.
- 불변식: 해시가 다르면 클라이언트가 `INTEGRITY_ERROR`. 어댑터는 이 호출에 없다.

## 문진 스키마 중 영역 경계를 넘는 8개의 파일 위치 (SV-14)

경로만 적는다(본문은 복사하지 않는다). 정본은 이 리포의 문진 스키마이며, 이 8개를 boundary-contracts로 옮기거나 변경 승인 절차를 정하는 것은 헤드 결정이다. 근거와 소비자는 점검 보고서 SV-14.

| 스키마 | 파일 | 경계를 넘는 이유(점검 보고서 SV-14) |
|---|---|---|
| FrontendMessage | `schema/schemas/FrontendMessage.json` | PUBLIC_API.md가 원문을 문진 스키마로 지정 |
| FrontendInput | `schema/schemas/FrontendInput.json` (`TurnFailureSignal` 포함) | 같음 |
| InterventionDecision | `schema/schemas/InterventionDecision.json` | 직원 콘솔 입력, PUBLIC_API.md |
| StaffSessionView | `schema/schemas/StaffSessionView.json` | 직원 콘솔 조회 응답, CR-013 |
| SessionResult | `schema/schemas/SessionResult.json` | 결과 조회 응답, CR-014 |
| RiskSignal | `schema/schemas/RiskSignal.json` | 외부 위험 신호 입력 |
| TurnTrace | `schema/schemas/TurnTrace.json` | ops-feedback이 `TraceEnvelope.trace`로 읽음(I6) |
| QuestionBank | `schema/schemas/QuestionBank.json` | question-authoring이 쓰고 이 영역이 읽음 |

(영역 안 8개 `SessionStore`, `OrchestratorInput`, `OrchestratorOutput`, `StaffConsoleInput`, `DispatchInput`, `DispatchOutput`, `QuestionFlow`, `ResponseTables`는 이 CR 대상이 아니다. `DispatchInput/Output`은 결과 전달 대상이 붙으면 경계가 된다.)

## 헤드에게 묻는 것
1. 위 6개 초안을 계약에 올릴지(1·2·3·4는 deploy와, 5는 turn-engine과, 6은 release-gate와 맞춰야 한다).
2. 문진 스키마 8개를 계약으로 옮길지, 변경 승인 절차로 둘지.
