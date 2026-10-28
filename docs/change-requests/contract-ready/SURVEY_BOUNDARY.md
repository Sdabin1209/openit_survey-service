# survey-service 문진 스키마 중 영역 경계를 넘는 것 (계약 승격 검토용, 2026-10-29)

헤드가 계약(boundary-contracts)으로 올릴지 정한다. 이 문서는 `schema/schemas/`(이 영역이 소유, D21)의 스키마 16개를 하나씩 보고 **다른 영역이 읽거나 쓰는가**를 적은 표다.

표기: **[확인]** 이 영역 코드나 계약 문서(`../boundary-contracts`)를 이 세션에서 읽은 것. **[미확인]** 이웃 영역의 코드·문서는 읽을 수 없어(규칙) 이웃이 실제로 쓰는지 모르는 것. "계약에 있나"는 `boundary_contracts/schemas/<이름>.schema.json` 파일이 있는가와 `PUBLIC_API.md`, `INTERFACES.md`, `HTTP.md`가 이름을 언급하는가를 grep한 결과다. 파일이 있는 것은 `TraceEnvelope` 하나뿐이다.

## 1. 경계를 넘는 스키마

| 문진 스키마 | 만드는 쪽 → 읽는 쪽 | 인터페이스 | 계약에 이미 있나 | 비고 |
|---|---|---|---|---|
| `FrontendMessage` | kiosk-frontend → survey-service | I10 `POST /v1/sessions`, `/turns`, `/end` 본문 [확인] | 이름만 언급(PUBLIC_API.md, INTERFACES.md, HTTP.md가 "문진 스키마의 FrontendMessage"라고 가리킴). 스키마 파일 없음 | 2.2.0에서 `SESSION_START`의 `session_id`가 선택이 됐다(서버 발급). 읽는 쪽 클라이언트는 `boundary_contracts.clients`가 필수 필드·타입만 본다(HTTP.md 137행) |
| `FrontendInput` | survey-service → kiosk-frontend | I10 위 호출의 응답 [확인] | 이름만 언급. 파일 없음 | 2.2.0에서 시작 응답에 선택 최상위 `session_id` 추가 |
| `TurnFailureSignal` (FrontendInput 안, 별도 파일 아님) | survey-service → kiosk-frontend | I10 응답의 `turn_failure_signal` [확인] | PUBLIC_API.md가 이름 언급 | 6개 스키마 파일에 같은 정의가 복사돼 있다 |
| `InterventionDecision` | admin-console(직원 콘솔) → survey-service [확인: PUBLIC_API.md 46행] | I10 `POST /v1/sessions/{id}/intervention` | 이름만 언급(PUBLIC_API.md). 파일 없음 | 직원 콘솔을 어느 영역이 만드는지는 이 영역 문서에 없다. PUBLIC_API.md는 kiosk-frontend의 직원 콘솔이라 적었고(INTERFACES.md 208행 부근) admin-console과의 관계는 [미확인] |
| `StaffSessionView` | survey-service → 직원 콘솔 [확인] | I10 `GET /v1/staff/sessions/{id}` | 이름 언급(PUBLIC_API.md, CHANGELOG.md). 파일 없음 | 2.2.0 `AlertKind` 이름이 목록과 같아졌다(CR-019) |
| `SessionResult` | survey-service → result-reader 역할 클라이언트 [확인] | I10 `GET /v1/results`, `/v1/results/{id}` | 이름 언급(PUBLIC_API.md). 파일 없음 | 읽는 영역이 어느 영역인지(예: ops-feedback)는 [미확인] |
| `RiskSignal` | 외부 감지기(risk-source 역할) → survey-service [확인] | I10 `POST /v1/sessions/{id}/risk-signals` | 이름 언급(PUBLIC_API.md). 파일 없음 | 만드는 영역이 지금 없다(외부 위험 감지기, CR-012). 읽는 쪽은 survey-service뿐 |
| `StaffConsoleInput` | survey-service → 직원 접점 | 이 영역 안 포트 `StaffConsole.deliver` [확인: ports.py]. 웹 푸시·SSE 페이로드는 이 스키마가 아니라 `StaffAlert`(계약) | 언급 없음 | 직원 알림의 영역 간 모양은 계약 `StaffAlert`/`StaffAlertPage`가 이미 정한다. 이 스키마는 영역 안 내부 형식으로 보인다 [판단] |
| `TurnTrace` | survey-service → ops-feedback | I6 `TraceEnvelope.trace` [확인: INTERFACES.md 117행, exporter.py] | 이름 언급(INTERFACES.md, HTTP.md, METRICS.md, README.md). `TraceEnvelope`는 파일이 있고 `trace`는 `runtime_contract_version`의 문진 스키마 `TurnTrace`로 검증한다 | ops-feedback이 정수 버전별 스키마를 어떻게 읽는지는 [미확인]. model-lab이 읽는지도 [미확인] |
| `QuestionBank` | question-authoring → release-gate → survey-service (그리고 model-runtime/model-lab이 같은 문진 스키마 정수 버전으로 학습·적재) | I1/I4 `get_question_bank`, 릴리스 번들 [확인: INTERFACES.md 31행, loader.py] | 이름 언급(CONVENTIONS.md, HTTP.md, INTERFACES.md). 파일 없음 | 정수 버전(`runtime_contract_version`) 소유는 survey-service(D19, D21). 어느 영역이 만들고 읽는지 전부는 [미확인] |
| `QuestionFlow` | survey-service 소유 데이터(`schema/data/question_flow.json`)의 스키마 | 영역 안(LangGraph와 turn-engine에 건네는 표) [확인: flow.py] | 언급 없음 | 승격 지시도 영역 몫이라 했다(`question-flow-table-args`). 영역 안 |
| `ResponseTables` | survey-service 소유 데이터(`schema/data/response_tables.json`)의 스키마 | 영역 안 [확인: response_tables.py]. model-runtime의 응답 문장 틀과 어휘가 같아야 한다는 시험이 있다 | 언급 없음 | 다른 영역이 이 파일을 읽는지는 [미확인] |
| `DispatchInput` / `DispatchOutput` | survey-service → 결과 전달 대상 → survey-service | 포트 `ResultDispatcher` [확인: ports.py]. 전달 대상은 "붙을 예정" 상태 | 언급 없음 | 현재 전달 대상이 없다(`dispatcher_status NO_TARGET`). 영역이 생기면 계약으로 올려야 한다 |
| `OrchestratorInput` / `OrchestratorOutput` / `SessionStore` | 영역 안(LangGraph 노드, 저장소) | 영역 안 | 언급 없음 | 경계를 넘지 않는다. `OrchestratorOutput`의 `turn_failure_signal`과 `log_entry`는 `TurnTrace`와 `StaffSessionView.turns`로 간접 노출된다 |

## 2. 요약: 헤드가 정할 것

1. **계약 파일이 없는 경계 스키마**: `FrontendMessage`, `FrontendInput`(`TurnFailureSignal` 포함), `InterventionDecision`, `StaffSessionView`, `SessionResult`, `RiskSignal`, `TurnTrace`, `QuestionBank`. 이름만 계약 문서가 가리키고 정의는 이 영역의 `schema/schemas/`다. 이웃은 `openit-survey-schema` 패키지(`survey_schema.load_schema`)나 `boundary_contracts`에 들어간 사본으로 읽는다. 계약 패키지에 사본을 올리면 두 곳에 정본이 생긴다. 그 선택(참조만 vs 사본 vs 소유 이전)이 헤드 몫이다. 지금 문서 규칙은 "survey-service가 소유하고 계약이 가리킨다"이다.
2. **영역 안으로 보이는 것**: `QuestionFlow`, `ResponseTables`, `OrchestratorInput`, `OrchestratorOutput`, `SessionStore`, `StaffConsoleInput`는 승격 불필요로 판단한다. `StaffConsoleInput`은 이웃과 맞닿는지 [미확인]이다.
3. **아직 상대가 없는 것**: `DispatchInput`/`DispatchOutput`(결과 전달 대상), `RiskSignal`의 생산자(외부 감지기).
4. **정수 스키마 버전**: `CONTRACT_VERSION`은 1이다. 2.2.0 대응(`AlertKind` 이름 변경, `FrontendMessage`/`FrontendInput` 변경)은 추가형이 아닌 변경이 하나 있다(`AlertKind`). 버전 정책은 헤드가 정한다.

## 3. 이 디렉터리의 승격 후보 4개(환경·출력·준비 상태·문항 세트 조회)

문진 스키마가 아니라 서비스 자체의 경계 형식이다. 목록은 `MANIFEST.json`, 검증은 `verify_contract_ready.py`.
