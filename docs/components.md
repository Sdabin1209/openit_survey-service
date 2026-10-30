# 컴포넌트 분해 (U400, DESIGN.md D117)

이 문서는 survey-service 영역을 **위에서부터** 나눈 결과다. 입력은 계약(boundary-contracts)과 브리프(`docs/brief.md`)에서 받고 내는 것이고, 기존 코드는 정의의 근거가 아니라 빠진 업무 규칙과 거부 조건을 찾는 대조 자료로만 썼다.

## 1. 위에서부터의 분해

영역이 받고 내는 것(브리프 2~3절, 계약):

- 받는 것: 키오스크의 세션 시작·발화·종료(I10, `FrontendMessage`), 직원의 개입(`InterventionDecision`)과 알림 조회, 활성 릴리스 묶음(I4), 위험 규칙 파일(`SurveyRiskRulesFile`).
- 내는 것: 키오스크에 줄 응답(`FrontendInput`), 추론 서버 호출(I9 extract·respond), Trace(`TraceEnvelope`, I6), 직원 푸시(`StaffPushPayload`), 보존 기간 삭제 결과(`SurveyPurgeOutput`).

이 일을 업무 단계로 나눈다.

1. **들어온 요청을 받아 계약으로 검증한다** (공개 API 받는 쪽).
2. **어느 처리 경로인지 정한다**: 입력 종류와 세션 상태로 처리 표의 칸을 고른다 (`turn_route`).
3. **세션을 읽고 쓴다**, 한 턴의 쓰기는 한 트랜잭션이다 (`session_store`).
4. **위험 문구를 가려낸다**: 모델 호출 전에 발화에서 (`risk_screen`), 걸리면 직원 알림을 만들고 전달한다 (`alert_book`, `alert_service`, `link_push`).
5. **모델 추출 입력을 만든다** (`extract_request`) -> 모델 호출 (`link_inference`) -> **서비스 고유 검사** (`extraction_check`) -> **저장할 값과 진행 계획** (`write_plan`) -> 쓰기 확정.
6. **되묻기 연속 상한**을 적용한다 (`reask_cap`).
7. **응답 입력을 만든다**: 표 찾기 (`response_request`) -> 모델 호출.
8. **결과를 정리한다**: 빈칸 원인 (`blank_details`), 결과 전달 입력 (`dispatch_input`).
9. **Trace를 봉투에 담아 보관하고 보낸다** (`trace_envelope`, `trace_outbox`, `link_trace_export`).
10. **릴리스를 읽어 적재한다** (`link_release_reader`, `release_loader`, `release_registry`), 문항 세트·문항 흐름의 보조 계산 (`question_bank_view`, `flow_table`, `field_specs`).
11. **보존 기간이 지난 것을 지운다** (`retention_purge`).

단계 사이를 잇는 일(어느 컴포넌트의 출력을 어느 입력에 넘길지, 한 턴의 순서, 자원 주입)은 컴포넌트가 아니라 `wiring/`의 몫이다(규칙 5).

## 2. 컴포넌트 표

파일은 `survey_service/components/` 아래다. 입력·출력 타입 이름은 `schemas/<타입>.json`의 이름이고 `survey_service/types/`에 같은 이름의 코드 타입이 생성된다. `계약:` 표시는 영역 사이 타입이라 복사하지 않고 boundary-contracts를 쓴다는 뜻이다(`x-contract`), `문진:`은 이 영역이 소유한 문진 스키마(`schema/`)의 타입이다(`x-survey-schema`).

### 2.1 업무 컴포넌트 (입력 스키마 하나 -> 출력 스키마 하나, 같은 입력이면 같은 출력)

| 컴포넌트 | 파일 | 입력 타입 | 출력 타입 | 하는 일 |
|---|---|---|---|---|
| turn_route | `components/turn_route.py` | TurnRouteQuery | TurnRoute | 입력 종류와 세션 상태(없음·진행·멈춤·끝남)로 처리 표(9-1)의 칸(턴 종류, 세션 생성 여부, 전체 흐름 여부, 로그 보존)을 고른다 |
| reask_cap | `components/reask_cap.py` | ReaskCapQuery | ReaskCap | 서비스 상한과 문항 세트가 정한 상한으로 되묻기 연속 상한의 실효 값을 계산한다 |
| flow_table | `components/flow_table.py` | FlowSource(문진: QuestionFlow) | FlowView | 문항 순서·건너뛰기 표를 만들고 건너뛴 문항, 직전 문항, 칸 순서를 묻는 질의에 답한다 |
| question_bank_view | `components/question_bank_view.py` | QuestionBankDoc(문진: QuestionBank) | BankView | 문항 세트를 order 순으로 정리하고 질문 문장·고정 문장을 찾는다 |
| field_specs | `components/field_specs.py` | QuestionBankDoc | FieldSpecs(계약: QuestionFieldSpec), FieldSpecIssues | 칸 정의에서 칸 규격을 만들고 계약을 어기는 칸을 가려낸다 |
| extract_request | `components/extract_request.py` | ExtractRequestSource | ExtractInput(계약: ActionExtractionInput) | 세션 문서·문항 세트·문항 흐름·발화에서 모델 추출 입력(state 포함)을 조립한다 |
| extraction_check | `components/extraction_check.py` | ExtractionCheckQuery | ExtractionViolations | 서버가 보증하지 못하는 두 가지(칸이 문항 세트에 있는가, 현재 문항인가)만 검사한다 |
| write_plan | `components/write_plan.py` | WritePlanQuery | TurnWriteSet | 추출 결과(save=true)에서 저장 갱신, 진행 갱신, 건너뜀을 계획한다 |
| response_request | `components/response_request.py` | ResponseRequestSource | ResponseInput(계약: ResponseRequest) | 표 찾기로 응답 입력(topic, known, ask, next)을 조립한다. 세션 시작과 일반 턴 |
| blank_details | `components/blank_details.py` | SessionDocument(문진: SessionStore) | BlankDetails | 값이 저장되지 않은 칸과 원인 코드(모름·거절·불분명)를 대화 로그에서 뽑는다 |
| dispatch_input | `components/dispatch_input.py` | SessionDocument | DispatchInput(문진: DispatchInput) | 세션 문서에서 결과 전달 입력을 고른다 |
| risk_screen | `components/risk_screen.py` | RiskScreenQuery | RiskScreenResult(계약: StaffAlert 또는 null) | 위험 규칙 파일을 읽고(`SurveyRiskRulesFile`) 발화 원문에서 규칙·문구 매치와 부정문 처리로 직원 알림을 만든다 |
| trace_envelope | `components/trace_envelope.py` | TraceEnvelopeSource | TraceEnvelopeOut(계약: TraceEnvelope) | TurnTrace를 봉투에 담고 바깥 ID를 `Id` 형식으로 바꾼다 |
| retention_purge | `components/retention_purge.py` | PurgeQuery | PurgeReport(계약: SurveyPurgeOutput에 쓰이는 개수) | 보존 기간이 지난 세션·알림·접근 기록을 지운다 |

### 2.2 상태·바깥 자원 컴포넌트 (작업 하나 = 입력 -> 출력 함수 모양으로만 드러낸다)

| 컴포넌트 | 파일 | 입력 타입 | 출력 타입 | 하는 일 |
|---|---|---|---|---|
| session_store | `components/session_store.py` | StoreLoadRequest, StoreCommitRequest | StoreLoadResult, StoreCommitResult | 세션 문서와 revision, 턴 멱등 기록을 SQLite·PostgreSQL·메모리에 읽고 쓴다. 쓰기 효과(저장 갱신·진행 갱신 적용)도 여기서 한다 |
| trace_outbox | `components/trace_outbox.py` | OutboxEnvelope(계약: TraceEnvelope) | OutboxEnqueueResult | Trace 봉투를 영속 버퍼에 넣고 전송 대상을 꺼내 상태를 바꾼다 |
| alert_book | `components/alert_book.py` | AlertWrite | AlertRead | 직원 알림, 조치, 푸시 구독, 접근 기록, 신호의 저장소 |
| alert_service | `components/alert_service.py` | AlertRaise | AlertRaised | 알림 생성의 멱등, 직원 호출 재시도, 수기 안내 보고, 알림 해소, 푸시 실패 처리 |
| release_registry | `components/release_registry.py` | ReleaseEntry | ReleaseLookup | 적재된 릴리스를 보관하고 활성 릴리스와 세션이 묶인 릴리스를 찾는다 |
| release_loader | `components/release_loader.py` | ActivePointer(계약) | PollResult | 활성 포인터를 보고 승인·해시·버전을 검사한 뒤 적재한다. 거부하면 이전 것을 유지한다 |

### 2.3 이웃 통신 컴포넌트 (HTTP와 이웃 SDK는 여기서만)

| 컴포넌트 | 파일 | 입력 타입 | 출력 타입 | 하는 일 |
|---|---|---|---|---|
| link_public_api | `components/link_public_api.py` | ApiRequest | ApiResponse | **받는 쪽.** 공개 API(I10)와 직원 면의 요청을 계약(`FrontendMessage`, `InterventionDecision`, `StaffAlertPage` 등)으로 검증하고 계약 타입 <-> 내부 타입으로 바꾼다. 인증 401, 권한 403, 오류는 `BoundaryError` 본문 |
| link_release_reader | `components/link_release_reader.py` | ReleaseReaderConfig | (계약 클라이언트 `HttpReleaseReader`) | **보내는 쪽.** 릴리스 읽기 SDK(I4) 클라이언트를 만든다 |
| link_trace_ingest | `components/link_trace_ingest.py` | TraceIngestConfig | (계약 클라이언트 `HttpTraceIngest`) | **보내는 쪽.** 수집 SDK(I6) 클라이언트를 만든다 |
| link_trace_export | `components/link_trace_export.py` | OutboxEnvelope | FlushResult | **보내는 쪽.** 버퍼의 봉투를 I6으로 보내고 최소 1회 전달, 재시도, DEAD 분류를 한다 |
| link_inference | `components/link_inference.py` | InferenceCall | InferenceResult(계약: RuntimeResult) | **보내는 쪽.** 추론 서버(I9)에 릴리스를 적재하고 extract·respond를 부른다. 재시도·마감·보호 건수 |
| link_push | `components/link_push.py` | PushDelivery(계약: StaffPushPayload) | PushOutcome | **보내는 쪽.** 직원 기기에 웹 푸시를 보낸다(SSRF 검사 포함) |

## 3. 컴포넌트 밖의 것

| 위치 | 담는 것 | 업무 규칙 |
|---|---|---|
| `wiring/turn_graph.py` | 한 턴의 LangGraph 그래프: 노드가 컴포넌트를 부르고 출력을 다음 입력에 넘긴다 | 없음 (연결만) |
| `wiring/orchestrator.py` | 세션별 직렬화, 멱등 재생, 마감, 자원 주입 | 없음 |
| `wiring/app.py` | 설정에서 컴포넌트를 만들어 이어 붙이는 조립, `/healthz` `/readyz` 모으기 | 없음 |
| `wiring/db.py`, `wiring/purge_job.py` | 저장소 선택·주입, 보존 삭제 잡의 진입 | 없음 |
| `wiring/errors.py`, `wiring/ports.py`, `wiring/pg.py`, `wiring/netguard.py` | 컴포넌트와 wiring이 함께 쓰는 공용 기반(예외 종류, 접점 Protocol, PostgreSQL 연결 어댑터, 주소 검사) | 한계 표 참조 |
| `server.py`, `__main__.py`, `settings.py`, `cli.py`, `migrate.py`, `migrations/` | 진입점, 환경 변수 설정 읽기, DB 마이그레이션 | 없음 |

## 4. 한계 (정직하게)

| 번호 | 한계 | 이유 | 다음 |
|---|---|---|---|
| L1 | 공용 기반 4개(`wiring/errors.py`, `ports.py`, `pg.py`, `netguard.py`)를 컴포넌트가 import한다 | 예외 종류와 DB 연결 어댑터를 컴포넌트마다 복제하면 `except StoreError`가 갈라진다. 컴포넌트끼리 import는 아니라 구조 검사 2번은 통과하지만 규칙 4(컴포넌트는 자기 스키마밖에 모른다)에는 못 미친다 | 예외를 각 컴포넌트 출력 스키마의 오류 종류로 바꾸는 일은 별도 작업 |
| L2 | 컴포넌트 사이에 같은 3~10줄 보조 함수(`model_key`, `item_of`, `slot_ref`)가 복제돼 있다 | import 금지(DESIGN 2절)와 코드 재사용이 부딪힌다. 표기 규칙(`Q7_1[soju].amount`)은 문진 스키마의 값 모양이라 각 컴포넌트가 입력 스키마에서 읽는다 | 문진 스키마가 키 구조를 필드로 내면 복제가 없어진다(문진 스키마 와이어 변경이라 오늘 하지 않는다) |
| L3 | 자세한 스키마 밖 조건은 `schemas/README.md` | | |
