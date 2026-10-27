# 경계 문서: survey-service

인터페이스 번호의 시그니처·오류·멱등성은 `../boundary-contracts/INTERFACES.md`와 `CONVENTIONS.md`가 정한다. 이 문서는 그 안에서 이 영역이 지키는 것이다.

## 1. 소유하는 것

- 소유 저장소: **Session Store**(서비스 내부, 기본 SQLite 파일 `SURVEY_SESSION_DB`)와 **Trace outbox**(SQLite 파일 `SURVEY_OUTBOX_DB`). 이웃은 두 파일을 읽지 않는다.
- 유지하는 것: `schema/`(문진 스키마, 정수 버전 `CONTRACT_VERSION`=1, 문항 순서·건너뛰기 표 `QuestionFlow`와 표 찾기 어휘표 `ResponseTables`(데이터셋 담당 규칙을 옮김, 확인 대기) 포함). 변경은 다른 영역의 변경 요청을 받아 반영하고, 계약 버전을 올리는 변경은 반영 전에 헤드에 알린다(D21).

## 2. 제공하는 것

| 제공 | 내용 |
|---|---|
| 문진 스키마 패키지 | 배포 이름 `openit-survey-schema`, import `survey_schema`. `load_schema(name, version=1)`, `schema_dir(version=1)`(model-runtime `load(schema_dir=...)`에 넘기는 `<계약명>.json`이 있는 디렉터리), `contract_version_dir`, `available_versions`, `current_version`, `list_schemas`. 정수 버전별 디렉터리 `survey_schema/v1/`. 설치: Git 태그 + `#subdirectory=schema`(태그는 헤드가 붙인다). |
| SDK 인터페이스 | **없다**(brief §2). 이웃이 서비스의 내부 저장소를 읽지 않는다. 따라서 이 영역의 메모리 구현(가짜 SDK)도 없다. 이웃이 이 영역을 흉내 낼 일은 공개 API(I10)가 정해진 뒤의 몫이다. |
| 공개 API(I10) | `boundary-contracts/PUBLIC_API.md`(D55): `POST /v1/sessions`, `POST /v1/sessions/{id}/turns`, `POST /v1/sessions/{id}/end`, `GET /v1/sessions/{id}/state`(키오스크), `POST /v1/sessions/{id}/intervention`(역할 staff, 멈춘 세션만), 직원 알림 `GET /v1/staff/alerts`, `GET /v1/staff/alerts/stream`(SSE), `GET /v1/staff/push-config`, `POST/DELETE /v1/staff/push-subscriptions`, 세션 기록 조회 `GET /v1/staff/sessions/{id}`(응답 `StaffSessionView`, 호출마다 접근 기록, PUBLIC_API.md C절)(모두 staff), 결과 조회 `GET /v1/results/{id}`·`GET /v1/results`(역할 result-reader, `SessionResult`, B절), 위험 신호 입력 `POST /v1/sessions/{id}/risk-signals`(역할 risk-source, `RiskSignal`, D절), 시작 본문의 선택 필드 `subject_ref`(A절), 개입의 `X-Openit-User` 헤더(E절), `GET /healthz`, `/readyz`. Bearer 토큰(키오스크/직원 두 개). 해석은 CR-009 |
| 개발용 CLI | `python -m survey_service.cli`(공개 API가 아니다) |

## 3. 소비하는 것과 지원 버전

| 소비 | 지원 |
|---|---|
| 문진 스키마(자기 `schema/`) | 정수 버전 **1** 한 값. `ReleaseManifest.runtime_contract_version`이 이 집합 밖이면 적재를 거부하고 이전 릴리스를 유지 |
| 경계 계약 | `boundary-contracts` **v2.0.2**(`pipeline_schema_version` "2.0", v1 레코드는 `UNSUPPORTED_VERSION`). 영역 사이 호출은 HTTP(`HTTP.md`), 클라이언트는 `boundary_contracts.clients`. 같은 MAJOR의 모르는 필드는 보존(`ext`)하고 판단에 쓰지 않는다 |
| I4 ReleaseReader | `get_active_pointer`(폴링), `get_release`, `get_decision`, `load_bundle`. 턴 처리 중에는 부르지 않는다 |
| I6 TraceIngest | `send(TraceEnvelope)`. 턴마다 한 건, 영속 outbox를 거쳐 |
| 웹 푸시 | pywebpush 2.5.0(잠금 고정). VAPID 키는 비밀 파일(`SURVEY_VAPID_PRIVATE_KEY_FILE`, D84), 없으면 SSE만 |
| 오케스트레이션 | LangGraph **1.2.15**(잠금 파일에 고정, 이미지 라벨 `io.openit.langgraph`). 외부 추적(LangSmith)은 이미지에서 끈다 |
| I8 turn-engine | **v0.4.0** (릴리스의 `turn_engine_version` MAJOR.MINOR가 다르면 적재 거부, PATCH는 경고) `validate_extraction`, `QuestionFlowTable`(문항 순서·건너뛰기 표는 이 영역이 문진 스키마에서 만들어 넘긴다, D76). 정규화기·판정기·기록기·Plan은 쓰지 않는다 |
| I9 model-runtime | **v0.7.1** (같은 규칙) HTTP 서버 `PUT /v1/releases/{id}`, `POST .../extract`(JSON), `POST .../respond`(text/plain). 매니페스트 기대값: `model_runtime_version` 0.7.x, 역할별 `system_prompt`, serving_config `prompt-4`(D77)·`protect-2`. `prompt-3`은 v0.7.0에서 제거되어(`prompt_template_versions=['prompt-4']`) 적재되지 않는다. 프로세스 안 `load`는 개발·스모크용 |

버전 고정: Git 태그(`requirements.txt`), 서드파티는 `requirements.lock`, 이미지는 `git archive <태그>`로 만든 wheel. 이미지 라벨: `org.opencontainers.image.revision`(커밋), `io.openit.contracts.boundary`, `io.openit.contracts.survey-schema`, `io.openit.turn-engine`, `io.openit.model-runtime`.

## 4. 이 영역이 이웃에게 지키는 것

- 포인터가 바뀌면 **새 세션부터** 새 릴리스를 쓴다. 진행 중 세션은 자기 릴리스로 끝까지 간다.
- 지원하지 않는 `runtime_contract_version`, 라이브러리 MAJOR.MINOR 불일치, 승인(PASS) 기록이 없거나 이 조합을 가리키지 않는 판정, 해시 불일치, 모델 적재 거부는 적재하지 않고 이전 릴리스를 유지한다.
- Trace: 턴마다 `TraceEnvelope`(릴리스 ID, 계약 버전 포함) 한 건. 전송은 턴 처리와 분리, `UNAVAILABLE`일 때만 재시도, 최소 1회 전달. 같은 (세션, 턴)은 한 번만 넣는다.
- 같은 (세션 ID, 턴 ID)의 재전송은 같은 결과(이중 저장 없음). 같은 턴 ID에 다른 입력이면 `TurnIdConflict`.
- 한 턴 동안 문항 저작·합성 데이터·모델 개발·게이트를 호출하지 않는다.

- **직원 알림 입력원 포트(G01)**: `survey_service.ports.RiskSource.check(*, extract_input, extract_output, utterance) -> Optional[Mapping]`. 발화가 검증을 통과한 턴마다 불리고, `StaffAlert`(`{"kind": "RISK"|"CALL_STAFF"|"USER_CALLED_STAFF", "reasons": [...]}`) 또는 None을 돌려준다. `RISK`는 세션을 정지시키고 고정 문장 `RISK_NOTICE`를 쓴다(reasons는 `RISK_SIGNAL`/`RISK_RULE`). 다른 kind는 정지 없이 알림만 낸다. 예외를 던지면 그 턴은 알림 없이 진행하고 `risk_source_error`를 센다. 같은 입력에는 같은 결과를 내야 하고 환자 발화 원문을 알림에 담지 않는다. 구현은 `SURVEY_RISK_SOURCE=모듈:함수`(인자 없는 팩토리)로 고르고, 기본은 없음(비활성)이다. 어떤 입력원을 쓸지는 정해지지 않았다. 설정만으로 쓸 수 있는 규칙 파일 입력원이 있다: `SURVEY_RISK_RULES_FILE`(`survey_service/risk_rules.py`, `SURVEY_RISK_SOURCE`와 함께 쓸 수 없다). 기본 규칙 파일은 없고 이 영역은 임상 문구를 담지 않는다. 걸리면 알림 이유의 evidence에 발화 원문 대신 `rule:<규칙 id>`만 담는다.
- **되묻기 상한(G12)**: `SURVEY_MAX_CONSECUTIVE_REASK`(기본 0=끔). 켜면 상한을 넘는 REASK·CONFIRM 대신 기존 직원 호출 경로(정지 `PAUSED_RISK`, `CALL_STAFF` 알림, 고정 문장 `CALL_STAFF`)를 쓴다. 값은 미정이다.

## 5. 이웃이 이 영역에 기대해서는 안 되는 것

- **직원 알림은 지금 만들어지지 않는다**: v2의 A 출력에 위험 신호가 없어 입력원이 없다(CR-012, 헤드 결정 대기). `/readyz`의 `staff_alert_source.active`가 false다. 알림 API·SSE·푸시는 살아 있다. 규칙 파일(`SURVEY_RISK_RULES_FILE`)을 주면 활성이 되고 `/readyz`의 `staff_alert_source`에 `rule_count`, `version`, `sha256`이 나온다. 규칙은 A 출력의 검증을 통과한 턴에서만 평가한다(검증 실패·A 호출 실패 턴의 발화에는 평가가 돌지 않는다. 한계).
- **환자 데이터 조회와 접근 기록**: 직원 세션 조회는 호출마다 (시각, 역할 이름, session_id, 결과)를 알림 DB의 `staff_access_log`에 남기고, 남기지 못하면 데이터를 주지 않는다. 시각은 알림 DB의 `session_timeline`에 턴마다 남긴다(세션 문서에는 시각이 없다). 알림 DB는 이 조회와 접근 기록의 원본이므로 백업 대상이다.
- **`/readyz`의 DB 확인(CR-D23)**: PostgreSQL이면 세 저장소를 **동시에**, 주 연결·락을 쓰지 않는 **별도 짧은 연결**(`connect_timeout`과 서버 `statement_timeout`이 `SURVEY_READYZ_DB_TIMEOUT_SECONDS`, 기본 2초)로 확인하고, 안 끝나는 확인은 +1초 뒤 실패로 센다. 이전에는 주 연결로 세 확인을 직렬로 했고 접속 시도마다 10초(`connect_timeout`)를 기다려 약 30초 이상 걸렸고, 보조 수치(outbox 건수, 구독 수) 읽기의 DB 예외가 처리되지 않아 연결이 끊겼다. 요청 경로(턴 처리, 재접속·재시도 규칙, 연결 timeout 10초)는 **바꾸지 않았다**. 기본 2초의 근거: 정상일 때 접속+질의는 수 ms이고, 로드 밸런서 검사 제한 시간이 보통 수 초이며, libpq의 `connect_timeout`은 2초 미만이 의미 없다. SQLite는 동작 불변(보조 수치만 같은 한도로 읽는다). 한계: 주 연결이 응답 없는 DB에 걸려 있으면 요청 경로는 여전히 그 접속 시도(최대 10초)만큼 기다린다.
- **저장소 두 가지(D85 2단계)**: `SURVEY_DATABASE_URL`(`read_secret`: `_FILE` 우선, 없으면 환경 변수)이 있으면 세션·Trace outbox·알림(알림, 시각, 접근 기록, 세션 메타, 위험 신호, 푸시 구독)이 모두 그 PostgreSQL 한 데이터베이스(deploy RUN.md 4-1-1의 `survey`)에 들어간다. 없으면 기존 SQLite 파일(`SURVEY_SESSION_DB`, `SURVEY_OUTBOX_DB`, `SURVEY_ALERT_DB`). 두 구현은 같은 SQL·같은 시험을 쓴다. 드라이버 `psycopg[binary]==3.3.6`. SQLite에서 PostgreSQL로 데이터를 옮기는 도구는 만들지 않았다(전환은 빈 데이터베이스에서 시작). `/readyz`의 `database.kind`로 종류를 본다(접속 문자열은 어디에도 나오지 않는다).
- **토큰 역할(서로 달라야 한다)**: kiosk, staff(필수), result-reader(`SURVEY_RESULT_READER_TOKEN[_FILE]`), risk-source(`SURVEY_RISK_SOURCE_TOKEN[_FILE]`). 뒤 둘은 값이 없으면 해당 경로가 꺼진다(404). 직원 개입은 게이트웨이가 붙이는 `X-Openit-User`가 없으면 401이고, 본문의 `staff_id`는 무시한다. 그 값은 대화 로그 개입 항목의 `staff_id`(LogEntry 선택 필드)와 알림 `resolved_by`에 남는다.
- **세션 종료 상태(결과 조회의 `status`)를 가르는 입력**: `ENDED_COMPLETE`는 환자 발화 턴에서 LoRA-A가 `next_item=END`를 낸 때뿐이다(마지막 문항 Q10까지 답한 흐름이 끝났을 때, `extraction.plan_writes`). `ENDED_LEFT`는 키오스크가 `POST /v1/sessions/{id}/end`(SESSION_END)를 보냈거나 유휴 만료(`SURVEY_SESSION_IDLE_SECONDS`)가 닫은 경우로, 문진이 끝나기 전에 환자가 떠난 것으로 본다(`graph.py`의 END_LEFT; 진행 중·정지 중 모두). `ENDED_STAFF`는 직원 개입 `END`. `ENDED_USER_STOP`은 스키마에 있으나 지금 코드가 만들지 않는다(예약, 미사용). 그래서 한 턴만 말하고 `/end`로 닫은 세션(위험 신호 → 직원 RESUME 뒤 포함)은 `ENDED_LEFT`가 맞고 의도다: 문진이 끝까지 가지 않았고 `/end`는 환자 이탈 신호이기 때문이다. **완료 여부는 `status == ENDED_COMPLETE`로만 판단한다.** `ENDED_LEFT`·`ENDED_STAFF`의 `answers`는 그때까지 저장된 값만 담은 부분 결과이고 `skipped`는 건너뛴 문항 ID일 뿐 미응답 문항 목록이 아니다. 응답에 별도의 완료 필드나 응답 문항 수는 없다(제안은 CR-014).
- **환자 참조와 보존**: `subject_ref`는 세션 문서가 아니라 알림 DB의 `session_meta`에 두고 로그·알림·푸시에 넣지 않는다. `SURVEY_SESSION_RETENTION_DAYS`(기본 없음=삭제 안 함)를 주면 `python -m survey_service purge`가 끝난 세션과 환자 참조·시각·신호 기록을 지운다(접근 기록은 지우지 않는다).
- **외부 위험 신호**: 효과는 내부 규칙 엔진과 같은 표기(`signal:<source>:<code>`, 알림 rule_id `source:code`)와 같은 정지(PAUSED_RISK)다. 내부 규칙 엔진은 source `internal`인 기본 감지기이고, 턴 안에서 평가되므로 정지·고정 문장이 그 턴의 쓰기와 한 트랜잭션이다. 외부 신호는 턴 밖에서 정지만 커밋한다(대화 로그 항목은 만들지 않고 정지 기록에 남는다).
- **결과 전달 대상 없음은 오류가 아니다**: 결과 전달은 붙을 예정이라 대상이 없는 것이 지금의 정상 구성이다. 세션이 끝날 때 INFO 로그 `결과 전달 대상 없음`과 `/readyz`의 `result_dispatcher: {attached: false, status: NO_TARGET}`로 드러내고, 세션의 전달 기록은 `succeeded=false`로 남는다. 대상을 꽂았는데 실패하면 그때는 `결과 전달 실패`(경고)다.
- 표 찾기(B 입력의 `topic`, `known`, `ask`, `previous`, `current`, `hold`)는 **데이터셋 담당의 규칙(학습 서버 labels.py, 2026-10-14)을 옮긴 것이고 데이터셋 담당의 확인 대기, 승인 기록 미확인**이다(`/readyz`의 `response_tables.status` = `DATASET_RULES_UNCONFIRMED`). 원본에 없는 서비스 쪽 판단은 57행 문제의 남은 칸 문장뿐이고 학습 분포 밖이다.

- 직원 개입은 위험으로 멈춘 세션에만 된다. 쓰기 반영 여부를 알 수 없는 커밋(`write_outcome: UNKNOWN`)은 결과를 고정하지 않으므로 같은 턴 ID 재시도가 다시 처리될 수 있다.
- 실패한 턴(`NOT_COMMITTED`)의 결과도 같은 턴 ID로는 같게 돌아온다. 다시 시도하려면 새 턴 ID가 필요하다 [제안, CR-003의 `turn_id` 항목과 함께 O06에서 확정].
- 직원 콘솔, 결과 전달, 프론트 전달의 실제 연동(접점 Protocol만 있다).

## 6. 장애 영향

| 상황 | 증상 | 영향 | 복구 |
|---|---|---|---|
| I4 일시 장애(`UNAVAILABLE`) | 폴링 실패 로그, `release_loader.last_poll_error` | 새 릴리스 반영이 늦어진다. 서비스와 진행 중 세션은 영향 없음 | 다음 주기에 자동 재시도 |
| 새 릴리스 적재 거부 | `rejections`에 코드, 이전 릴리스 유지 | 새 릴리스가 안 켜진다 | 게이트·모델 쪽을 고치고 새 포인터 |
| 활성 릴리스가 한 번도 적재되지 않음 | `/readyz` 503 `no_active_release` | 새 세션을 받을 수 없다 | 포인터·릴리스 확인 |
| I6(수집) 장애 | outbox `pending` 증가, `consecutive_unavailable` | **턴 응답에는 영향 없음**. Trace 전달이 늦어진다 | 수집 복구 후 자동 전송 |
| outbox 디스크 오류 | Trace 유실(턴은 정상), `trace_enqueue_failed` 카운터, `/readyz` 503 `outbox_db` | 해당 Trace 없음(최소 1회 보장 밖) | 디스크 복구 |
| PostgreSQL 중지·연결 불가·응답 없음(CR-D23) | `/readyz`가 **`SURVEY_READYZ_DB_TIMEOUT_SECONDS`(기본 2초) + 약 2초 안에** 503, `problems`에 `session_db`/`outbox_db`/`alert_db`, `database: {kind, status: UNAVAILABLE, detail: "db unavailable"}`(접속 정보 없음). `/healthz`는 200 | 새 세션·턴은 저장소 오류 경로(아래 행)를 따른다. 로드 밸런서가 `/readyz`로 빼낼 수 있다. DB가 돌아오면 서비스를 다시 띄우지 않아도 다음 `/readyz`가 200 | 자동. `trace_outbox`·`staff_alerts` 수치는 주 연결이 살아날 때까지 마지막 값에 `db_stats_stale: true` |
| Session Store 오류 | 쓰기 확정 실패 → `NOT_COMMITTED` 신호, 응답 없음. 조회 실패는 로그 없는 실패 신호 | 그 턴은 반영되지 않는다. 확정 뒤의 일은 하지 않는다(위험 알림도 나가지 않는다, 아래 주의) | 저장소 복구 후 새 턴 |
| 직원 콘솔 전달 실패 | `DELIVERY_FAILED` 신호, 응답은 그대로 | 쓰기는 반영됨. 직원이 알림을 받지 못함 | 프론트가 신호로 대처(규칙 J3) |
| 모델 출력이 깨짐 | model-runtime의 출력 보호(대체 출력) | 턴은 실패하지 않음 | model-lab이 `*_raw`로 품질 확인 |
| 컨테이너 재시작 | 진행 중 세션 유지(SQLite), 릴리스 재적재 | 재적재 전까지 `/readyz` 503 | 자동 |

**주의(안전)**: 쓰기 확정이 실패한 턴에서는 위험 신호가 있어도 직원 알림을 보내지 않는다. 규칙(preconditions 0-1.3, J8f)이 그렇게 정한다. 프론트는 `NOT_COMMITTED` 신호를 받으면 사용자에게 다시 말하게 하거나 직원 호출로 대처해야 한다. 이 정책이 위험 상황에서 충분한지는 도메인 소유자가 확인할 일이다.

## 7. 소비자별 메모

| 이웃 | 메모 |
|---|---|
| release-gate | 활성 포인터는 값싸게 자주 읽힌다(기본 5초). 매니페스트 `content_hash`와 `question_set.content_hash`를 서비스가 다시 계산해 검증한다. `adapters.LORA_A/B.serving_config`가 달라지면 거부된다(model-runtime 0.3.0 전까지, CR-006). `turn_engine_version`·`model_runtime_version`은 이미지에 설치된 것과 MAJOR.MINOR가 같아야 한다. |
| ops-feedback | 봉투의 `release_id`는 세션이 묶인 릴리스다. `Id` 형식이 아닌 세션 ID·턴 ID는 `x-`+SHA-256 앞 32자로 바꿔 봉투에 넣는다(CONVENTIONS 10절). 원본은 `trace` 본문에 있다. 같은 (세션, 턴)이 두 번 올 수 있다(최소 1회). |
| 다른 영역의 문진 스키마 소비 | `openit-survey-schema`를 설치한다. 정수 버전을 여러 개 지원하면 `load_schema(name, version=N)`. |
