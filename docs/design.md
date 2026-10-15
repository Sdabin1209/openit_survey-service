# survey-service 영역 설계서

표기: **[확인]** 계약·브리프·규칙 문서에서 읽은 것, **[결정]** 이 영역이 정한 내부 설계(경계에 닿지 않음), **[제안]** 규칙 문서에 없어 이 문서가 고른 해석(바꿀 수 있음, 필요하면 변경 요청), **[미정]** 정해지지 않은 것, **[O06 대기]** 공개 API가 정해져야 하는 것.

## 1. 범위

- 하는 일 [확인: brief §1]: 한 턴을 처리하는 Orchestrator, Session Store, 릴리스 로더, Trace 내보내기, 추론 서버, 문진 스키마 패키지(`schema/`).
- 하지 않는 일 [확인]: 판단 로직(turn-engine), 모델 로딩·프롬프트·디코딩(model-runtime), 문항·모델 승인(게이트), Frontend 화면.
- **공개 API(I10)** [확인: D55, `boundary-contracts/PUBLIC_API.md`]: 키오스크 프론트가 부르는 HTTP/JSON. `POST /v1/sessions`(시작), `POST /v1/sessions/{id}/turns`(발화, `turn_id` 멱등), `POST /v1/sessions/{id}/end`(종료), `GET /healthz`, `/readyz`. Bearer 토큰(환경 변수의 이름으로 지정, `SURVEY_API_TOKEN_ENV`). 구현은 `survey_service/api.py`가 기존 `handle_turn`(엔진·기록·직원 호출·결과 전달·Trace)에 그대로 연결하고, 문서가 정하지 않은 점의 해석은 CR-009에 적었다. 요청은 `FrontendMessage`로 검증하고 응답은 `FrontendInput`이다. 상태 코드: 400/401/404/409/429/503. 세션 유휴 만료(기본 300초, `SURVEY_SESSION_IDLE_SECONDS`)는 이탈 종료로 닫는다. 개발용 CLI(`python -m survey_service.cli`)는 그대로 있다. 직원 개입은 `POST /v1/sessions/{id}/intervention`(역할 `staff` 토큰, 키오스크 토큰은 403, 위험으로 멈춘 세션만, 아니면 409, `turn_id` 멱등)이 `InterventionDecision` 처리에 연결된다 [확인: D58, v1.3.1].

## 2. 컴포넌트 지도

```
              +-------------------- survey_service (프로세스) ---------------------+
 handle_turn  | Orchestrator --- Engine(turn-engine: normalize/judge/write_record/plan)
 (라이브러리) |      |      \-- Inference(model-runtime ModelSession: parse/generate)
              |      |                         ^
              |      v                         | 적재
              | Session Store (SQLite/메모리)  Release Loader --- I4 ReleaseReader (읽기 SDK)
              |      |  (세션 문서, 턴 결과, 릴리스 바인딩)
              |      v
              | Trace Outbox (SQLite) --- Exporter 스레드 --- I6 TraceIngest (수집 SDK)
              | HTTP: /healthz /readyz
              +---------------------------------------------------------------------+
 바깥 접점(Protocol, 구현은 주입): StaffConsole, ResultDispatcher, FrontendSink
```

| 모듈 | 책임 |
|---|---|
| `survey_service.orchestrator` | 처리 표(9-1), 턴 순서, 쓰기 확정, 확정 뒤의 일, 실패 신호, 로그, 멱등 |
| `survey_service.store.effects` | 저장소 반영 효과(9-2)를 순수 함수로. 판단 없음 |
| `survey_service.store.sqlite` / `memory` | Session Store 구현 두 개. 같은 적합성 시험을 통과 |
| `survey_service.release` | 활성 포인터 폴링, 번들 적재, 거부, 이전 릴리스 유지, 릴리스 레지스트리 |
| `survey_service.inference` | 릴리스마다 ModelSession을 만들고 보관(추론 서버), 백엔드 선택 |
| `survey_service.trace` | TurnTrace 조립, 봉투, 영속 outbox, 전송 스레드 |
| `survey_service.service` | 조립, 설정(환경 변수), `/healthz` `/readyz` |
| `schema/survey_schema` | 문진 스키마 패키지(별도 배포 단위) |

## 3. 한 턴의 흐름

입력은 `FrontendMessage`(SESSION_START, UTTERANCE, SESSION_END) 또는 `InterventionDecision`(RESUME, END) 중 하나와 호출자가 주는 `turn_id`다. [제안] `turn_id`는 호출자(나중에 공개 API)가 정하고, 같은 `(session_id, turn_id)`를 다시 받으면 저장한 결과를 그대로 돌려준다(이중 저장 없음, brief §4). 같은 턴 ID에 다른 입력이 오면 `TurnIdConflict`.

1. **세션별 직렬화** [확인: preconditions 0-1.1]: 세션마다 락. 앞 턴이 끝나기 전의 입력은 기다린다. 여러 프로세스로 늘릴 때를 위해 커밋은 `expected_revision`으로 낙관적 검사를 한다(`REVISION_CONFLICT`면 쓰기를 반영하지 않고 턴 실패).
2. **턴 맨 앞 조회**: 저장소에서 세션을 찾는다(`SessionLoadRequest/Result`). `OrchestratorInput`을 만든다. `latest_question_bank_version`은 현재 활성 릴리스의 문항 세트 버전이다.
3. **처리 표(9-1)**:

| 입력 \ 세션 | 없음 | 진행 중 | 일시 중지 | 종료 |
|---|---|---|---|---|
| SESSION_START | 세션 생성 후 Plan(SESSION_START) | Plan(SESSION_START) | 처리 안 함(로그 남김) | 처리 안 함(로그 없음) |
| UTTERANCE | 세션 생성 후 전체 흐름 | 전체 흐름 | 처리 안 함(로그 남김) | 처리 안 함(로그 없음) |
| RESUME | 처리 안 함 | 처리 안 함 | Plan(RESUME) | 처리 안 함 |
| END | 처리 안 함 | Plan(INTERVENTION_END) | Plan(INTERVENTION_END) | 처리 안 함 |
| SESSION_END | 처리 안 함 | Plan(END_LEFT) | Plan(END_LEFT) | 처리 안 함 |

   "처리 안 함"은 어떤 컴포넌트도 부르지 않고 응답이 없다. 개입 결정이면 `intervention_result = IGNORED`. 로그 규칙은 9-1의 3항.
4. **세션 생성**: 초기 `SessionStore`(모든 슬롯 NONE, 진행 기록·Plan 기록 초기값)를 만들고 현재 활성 릴리스에 묶는다(새 세션부터 새 릴리스, O13). 실패하면 그 턴은 로그 없이 `NOT_COMMITTED` 신호(J8a).
5. **전체 흐름(발화)**: Parser(LoRA-A) → normalize → judge → write_record → (저장 결정을 메모리에서 반영) → plan. 어느 단계든 예외면 `BEFORE_COMMIT` 실패로 멈추고 이후 단계를 하지 않는다(규칙 4: 저장 판단 뒤에는 앞으로 돌아가지 않음). Plan만 부르는 턴은 LoRA-A~저장 판단을 건너뛴다.
6. **쓰기 확정**: 저장 결정 + 진행 기록 갱신 + Plan 기록 갱신을 한 번에 커밋한다(`SessionCommitRequest.writes`). 실패하면 아무것도 반영하지 않는다. 실패 시 확정 뒤의 일은 하나도 하지 않는다(preconditions 0-1.3, J8f).
7. **확정 뒤의 일(`AFTER_COMMIT`)** 은 서로의 성패와 무관하게 모두 시도한다: 직원 알림 전달(Plan이 `staff_alert`를 냈을 때), 결과 전달(`flow == END`), 응답 생성(LoRA-B), 프론트 전달. 직원 알림 전달은 지연 없이 하기 위해 응답 생성보다 앞에 한다.
8. **로그와 신호**: 대화 로그(`LogEntry`)는 턴 맨 끝에 따로 쓴다(실패한 턴도). 실패 단계가 하나라도 있으면 `TurnFailureSignal{write_committed, staff_alert}`를 프론트·직원 콘솔·출력에 싣는다. 개입 결정이면 `intervention_result`는 쓰기가 반영되면 APPLIED, 반영되지 않으면 FAILED, 처리 안 함이면 IGNORED.
9. **Trace**: 턴이 끝나면 `TurnTrace`를 조립해 `TraceEnvelope`(릴리스 ID, 계약 버전 포함)로 outbox에 넣는다(§6). 이 한 번의 로컬 쓰기 외에 턴 응답이 전송을 기다리지 않는다.

### 3.1 컴포넌트 입력 조립

필드마다 원천 계약·필드·읽는 시점(snapshot)·전달 방식은 `docs/rules/sources/bindings.yaml`(헤드 전달, 원본 `work/contracts/`)이 정한다 [확인]. `tests/test_bindings.py`가 조립한 입력을 이 표와 필드 단위로 대조한다: 필드 집합이 표와 같은지, turn_start와 post_store_write 시점, project/lookup/copy 방식, 판정기 `pendings`에 `value`·`risk_mark`가 없고 정규화기 `pendings`에 `risk_mark`가 없는 것 등. 처음 대조에서 다른 곳은 하나였다(판정기 `affected_followups`를 비어 있지 않은 문항만 냈는데 표는 `questions[]` 전부의 투영이다. 고쳤다). `AnswerNormalizerInput.awaiting`은 표가 "last_directive.awaiting"이라 하지만 계약이 `say_values`를 허용하지 않아 `{action, targets, target_pending_id}`로 투영한다.

표가 정하지 않고 `selection_owner`가 Orchestrator인 **선택 정책**은 이 영역이 정했다 [결정]:

| 입력 필드 | 표가 정한 것 | 이 영역의 선택 정책과 근거 |
|---|---|---|
| `UtteranceParserInput.awaiting_questions` | 직전 지시 `awaiting.targets`가 참조하는 문항 정의를 중복 없이 | 문항 `order` 순. 순서는 표에 없어, 같은 입력이 같은 출력을 내도록(결정성) 정렬한다 |
| `UtteranceParserInput.answered_slots` | 현재값이 `{answer}`인 행 | 반복 키 슬롯 포함 모든 행. 모름·거절·없음은 답이 아니다(9-2) |
| `AnswerNormalizerInput.related_questions` | 선택자 `item_target_questions`, `awaiting_slot_questions`, `direct_prerequisite_questions` | 선택자 세 개의 합집합을 문항 `order` 순으로. 직접 선행만(선행의 선행은 넣지 않는다) |
| `current_values`, `pendings` | "선택한 answer_records" | 관련 문항에 속한 모든 슬롯 행(반복 키 포함). 행 단위로 고르면 같은 문항의 일부 슬롯만 보여 누락 판정이 흔들린다 |
| `AnswerJudgeInput.pendings` | 확인 대기가 있는 선택 행 | 세션 전체의 확인 대기. 판정기가 확인 응답을 짝짓는 데 관련 문항 밖의 대기도 필요할 수 있어 줄이지 않는다 |
| `RecordWriterInput.target_records` | 판정 대상·`invalidated_questions`에 해당하는 슬롯 기록 | 대상이 문항 전체면 그 문항의 모든 슬롯(반복 키 포함) |
| `turn_index` (표: 선택 주체 Framework) | 호출 문맥의 순번, Trace의 `turn_index`와 같게 | 턴 시작 때 대화 로그 항목 수(처리 안 함 턴 포함). 정규화기 입력과 Trace에 같은 값 |

### 3.2 저장소 반영 효과

`survey_service.store.effects`가 9-2의 두 표를 그대로 구현한다. 판단 없이 효과만 적용한다. 반영할 수 없는 결정(없는 슬롯, 맞지 않는 확인 대기 ID)은 `StoreEffectError`로 커밋을 거부한다(쓰기 전체가 반영되지 않는다). 규칙 문서의 모호한 곳은 아래처럼 해석했다.

| 모호한 곳 | 해석 [제안] |
|---|---|
| 건너뜀 추가와 해제가 같이 올 때 | 해제를 먼저(규칙 명시). 확인 중단도 같은 순서로 |
| 확인 중단 추가가 이미 있는 슬롯 | 중복 없이 한 번만 |
| 이력에 쌓는 "이전 답" | 현재값이 값(answer)일 때만. 모름·거절·NONE은 쌓지 않는다. 이전 값에 `value_metadata`가 있으면 `{value, value_metadata}` 항목 |
| 첫 저장·새 revision의 `value_metadata` | 저장 결정에 있으면 슬롯 기록에 둔다. 없으면 슬롯 기록에서 지운다(이전 값의 정밀도를 새 값에 붙이지 않는다) |
| 확인 대기 받아들임 | 확인 대기 기록의 `value`, `risk_mark`, `value_metadata`가 현재값·위험 표시·메타데이터가 된다 |

## 4. Session Store

[결정] 인터페이스(`SessionStorePort`) 뒤에 두 구현을 둔다: SQLite(기본, 단일 파일, WAL)와 메모리(시험·개발). 세션마다 하나의 `SessionStore` 문서(JSON)와 정수 `revision`을 가진다. 문서에는 내부 정보를 넣지 않는다(스키마 위반). 내부 정보(릴리스 바인딩, 턴 결과)는 별도 테이블에 둔다.

| 테이블 | 내용 |
|---|---|
| `sessions(session_id PK, revision, release_id, created_at, status, doc)` | 문서와 낙관적 revision, 세션이 묶인 릴리스 ID, 비정규화한 진행 상태 |
| `turns(session_id, turn_id, input_hash, committed, result_json)` | 턴 멱등 기록. 커밋과 같은 트랜잭션에서 `committed=1`, 턴 맨 끝에서 `result_json` |

- `load(session_id)` → `SessionLoadResult`(OK/NOT_FOUND/DB_ERROR). `commit(request, turn)` → `SessionCommitResult`(OK/REVISION_CONFLICT/DB_ERROR, `write_outcome` APPLIED/NOT_APPLIED/UNKNOWN). 이 둘은 문진 스키마의 `SessionLoadRequest` 등이다. 이 계약은 `COMPONENTS.yaml`에서 `proposal` 상태이고 TurnTrace에만 정의돼 있다. 경계에 닿지 않는 내부 사용이므로 그대로 쓰고 변경 요청은 내지 않는다(제안 상태가 바뀌면 따라간다).
- 한 턴의 쓰기는 한 SQLite 트랜잭션. 대화 로그·전달 기록 추가는 따로 쓴다(preconditions 0-1.2).
- 개인정보 [미정, O06]: 문진 대상 참조가 무엇인지 정해지지 않아 세션 문서에 신원 정보를 넣지 않는다. 암호화·보존 기간은 정해지지 않았다.
- 커밋이 반영됐는데 응답 전에 죽은 경우: `turns.committed=1`이고 `result_json`이 없다. 같은 턴 ID 재시도는 쓰기를 다시 하지 않고 "반영됨, 프론트 전달 실패"(`AFTER_COMMIT/DELIVER_FRONT`, COMMITTED) 결과를 낸다. 응답 문장을 다시 만들지 않는 이유는 모델 호출 결과가 같다는 보장이 없고, 전달 단계가 O06 대기이기 때문이다 [제안].

## 5. 릴리스 로더

- 주기(`SURVEY_RELEASE_POLL_SECONDS`)로 `get_active_pointer()`를 부른다. `pointer_version`이 마지막으로 본 값과 같으면 아무것도 안 한다 [확인: I4].
- I4는 HTTP 클라이언트(`HttpReleaseReader`)다. `load_bundle(release_id, dest=작업 디렉터리)`가 `dest/<릴리스 ID>/`에 번들을 풀고 모든 해시를 검증하며, 로더는 그 경로의 어댑터와 돌려받은 질문지를 쓴다. 클라이언트 오류는 `BoundaryError`다: `UNAVAILABLE`(네트워크, 5xx, 503)이면 이전 릴리스를 유지한 채 같은 포인터를 다음 주기에 다시 본다. `INTEGRITY_ERROR`, `UNSUPPORTED_VERSION`, `FAILED_PRECONDITION`, `NOT_FOUND`는 그 포인터를 거부로 기록한다.
- 달라졌으면 `get_release` → 검사 → `load_bundle` → 검사 → 모델 적재 순서. 검사:
  1. `runtime_contract_version`이 지원 값(이 이미지가 설치한 문진 스키마의 정수 버전 집합)에 없으면 **적재하지 않고 이전 릴리스를 유지**한다 [확인: I4]. 같은 포인터 버전은 다시 시도하지 않는다.
  2. `gate_decision_id`의 `GateDecision`이 PASS이고 이 조합을 가리키는지 확인한다(승인 기록 없는 묶음 거부, brief §4). `load_bundle`도 통과하지 않은 릴리스를 `FAILED_PRECONDITION`으로 거절한다.
  3. 문항 세트가 `QuestionBank` 스키마를 통과하고 `manifest.question_set.content_hash`와 해시가 같은지, `manifest.content_hash`가 맞는지 확인한다(기준 구현 `boundary_contracts.canonical`).
  4. `manifest.turn_engine_version`, `model_runtime_version`의 **MAJOR.MINOR가 설치된 값과 다르면 적재를 거부**하고 이전 릴리스를 유지한다(`VERSION_MISMATCH`, 사유는 `/readyz`의 `release_loader.rejections`). **PATCH만 다르면 경고만 하고 적재한다**. 게이트가 승인한 동작과 서비스 동작이 달라지지 않게 하기 위해서다 [확인: 헤드 결정 D45, CR-005]. 해석할 수 없는 버전 문자열도 거부한다.
  5. `model_runtime.load`가 거부하면(`ServingConfigMismatch`, `UnsupportedServingConfig`, `ContractVersionMismatch`, 백엔드 없음) 적재 실패로 보고 이전 릴리스를 유지한다.
- 성공하면 레지스트리에 넣고 활성 릴리스를 바꾼다. **새 세션만** 새 릴리스에 묶인다. 진행 중인 세션은 자기 릴리스로 끝까지 처리한다(세션은 `release_id`를 가진다).
- **턴을 처리하는 동안 이웃(I4)을 부르지 않는다** [확인: brief §4]. 그래서 서비스를 다시 띄운 뒤 진행 중 세션이 가리키는 릴리스는 시작할 때와 폴링 주기마다 턴 밖에서 `ensure_loaded`로 미리 적재한다. 적재하지 못한 릴리스는 `/readyz`의 `release_loader.unavailable_releases`에 나오고, 그 세션의 턴은 첫 단계 실패(`BEFORE_COMMIT/LoRA-A`)로 처리한다 [제안]. 새 세션을 받는 능력(준비 여부)에는 영향이 없다.
- 열린 세션이 없고 활성이 아닌 릴리스는 최근 `SURVEY_KEEP_RELEASES`개를 남기고 내린다.
- 상태: 활성 릴리스 ID, 마지막 포인터 버전, 거부 목록(사유 코드, 포인터 버전). `/readyz`와 런북에서 본다.
- 첫 활성 포인터가 없으면(`None`) 서비스는 살아 있으나 준비되지 않았다(`/readyz` 503).

## 6. Trace 내보내기

- 턴마다 `TurnTrace` 하나. 조립 규칙 [확인: trace_contract]: 각 경계는 해당 계약의 전체 payload 또는 null. null은 미확보(예: 처리 안 함 턴의 컴포넌트 입출력). `session_before`/`session_after`는 턴 시작 문서·턴 끝 문서, `persistence_*`는 조회·커밋 요청과 결과.
- `TraceEnvelope`: `pipeline_schema_version`(boundary-contracts가 정한 값), `release_id`(세션이 묶인 릴리스, 세션이 없으면 활성 릴리스), `runtime_contract_version`, `session_id`, `turn_id`, `sent_at`, `trace`.
- **외부 ID 변환** [확인: CONVENTIONS 10절, CR-007]: 세션 ID·턴 ID가 `Id` 형식(`[A-Za-z0-9][A-Za-z0-9._:-]*`, 128자)이면 그대로, 아니면 `x-` + 원본 UTF-8 SHA-256 앞 32자(소문자 16진)를 봉투에 넣는다. 결정적이라 outbox의 (세션, 턴) 유일성과 수집의 멱등이 유지되고, 원본은 `trace` 본문에 남는다. 서비스 안의 세션·턴 식별(저장소, 멱등)은 원본 ID를 쓴다. 공개 API(O06)가 `Id` 형식으로 발급하기로 정해지면 변환은 쓰이지 않는다.
- **outbox** [결정]: SQLite 파일(`SURVEY_OUTBOX_DB`). `(session_id, turn_id)` 유일. 턴 처리는 `enqueue`(로컬 INSERT) 한 번만 한다. 전송 스레드가 별도로 `send`를 부른다.
- 재시도 [확인: I6, CONVENTIONS 4]: `BoundaryError`의 `retryable`(UNAVAILABLE)일 때만 지수 백오프로 재시도한다. `ACCEPTED`와 `DUPLICATE`는 둘 다 전달 완료로 본다(최소 1회 전달, 수집이 키로 멱등). 재시도 불가 오류(예: `INVALID_RECORD`, `UNSUPPORTED_VERSION`)는 `DEAD`로 옮겨 재시도하지 않고 개수를 상태에 낸다. `BoundaryError`가 아닌 예외는 선언된 실패가 아니므로 `UNAVAILABLE`처럼 보류하되 `unexpected`로 표시한다 [제안].
- outbox 쓰기 실패는 턴을 실패시키지 않는다. 표준 오류로 알리고 카운터를 올린다(그 Trace는 잃는다. 이 경우 최소 1회 보장은 깨진다. 디스크 오류와 같은 급이라 비보증으로 둔다 [제안]).
- 보존: 전달 완료 행은 `SURVEY_OUTBOX_KEEP_SENT_HOURS` 뒤에 지운다.
- 크기 [미정]: `session_before`/`session_after`/`persistence_load_result`가 세션 문서 전체(대화 로그 포함)를 담아 한 세션 안에서 턴이 늘수록 Trace가 커진다(턴 수에 거의 비례). 스키마가 정한 필드라 줄이지 않았다. 문제가 되면 ops-feedback과 협의해 문서에서 대화 로그를 뺀 형태를 변경 요청으로 낸다. 발화 원문이 Trace에 들어가므로 비식별은 수집 쪽(D15)이 한다.

## 7. 추론 서버

- **HTTP 경계** [결정, D55 후속; D62 정리]: 서비스는 모델을 적재하거나 호출하지 않고 `SURVEY_INFERENCE_URL`로 **HTTP 호출만** 한다(`survey_service/remote.py`). 추론 서버 규약(요청·응답, 오류, 릴리스 적재 신호, 실패 처리)은 `docs/inference-server-spec.md`이고, 서버(vLLM 컨테이너)는 model-runtime 영역이 구현한다(CR-010). 서비스 안에 있던 추론 서버 코드는 서비스 패키지에서 뺐고, 참조 구현은 모델 없는 시험 대역이다(`tests/ref_inference_server.py`). 호출 실패·지연·잘못된 출력의 처리는 규약 5절이고 `tests/test_inference_client.py`가 느린 서버·죽은 서버·틀린 출력까지 확인한다. 출력 보호(D36)는 서버 쪽 model-runtime의 책임이고 서비스는 출력 계약을 확인만 한다(어기면 서버 결함으로 그 단계를 실패시킨다). `/readyz`의 `inference`에 호출·실패·재시도·타임아웃·보호 건수가 나온다. 프로세스 안 적재(`SURVEY_MODEL_BACKEND=fake`, `InferenceServer`)는 개발·스모크용으로 남아 있다.
- 역할별 serving_config [확인: 헤드 결정 D46, CR-006, model-runtime v0.3.0]: (프로세스 안 적재일 때) `ReleaseManifest.adapters[role]`의 `serving_config`·`serving_config_hash`를 역할별로 I9 `load(adapters, serving_configs, expected_serving_config_hashes)`에 그대로 넘긴다(`inference.role_configs`). 한 역할의 해시만 어긋나도 model-runtime이 `ServingConfigMismatch`(역할 표시)로 거부하고 이전 릴리스를 유지한다. 두 역할은 한 백엔드를 공유한다. 기반 모델이 다른 두 어댑터는 백엔드 구현 때 정해진다 [미정]. 이전의 `SERVING_CONFIG_DIFFERS_BY_ROLE` 거부는 없앴다.
- 백엔드 선택: `SURVEY_MODEL_BACKEND`. 지금 구현된 값은 `fake`(개발·스모크 전용, 운영 거부 표시)뿐이다. 값이 없으면 시작하지 않는다.

## 8. 서비스와 설정

| 환경 변수 | 뜻 |
|---|---|
| `SURVEY_PORT` (8080) | `/healthz`, `/readyz` 포트 |
| `SURVEY_SESSION_DB` (/data/sessions.db) | Session Store |
| `SURVEY_OUTBOX_DB` (/data/outbox.db) | Trace outbox |
| `SURVEY_RELEASE_READER` | `http`(기본: `boundary_contracts.clients.HttpReleaseReader`, D49) 또는 팩토리 `모듈:함수`. `SURVEY_RELEASE_READER_URL`, 토큰이 든 환경 변수의 이름 `SURVEY_RELEASE_READER_TOKEN_ENV` |
| `SURVEY_TRACE_INGEST` | `http`(`HttpTraceIngest`) 또는 팩토리. `SURVEY_TRACE_INGEST_URL`, `SURVEY_TRACE_INGEST_TOKEN_ENV` |
| `SURVEY_MODEL_BACKEND` | `fake` 등 |
| `SURVEY_RELEASE_POLL_SECONDS` (5) | 포인터 폴링 주기 |
| `SURVEY_KEEP_RELEASES` (2) | 내리지 않고 남길 비활성 릴리스 수 |
| `SURVEY_EXPORT_INTERVAL_SECONDS` (1), `SURVEY_EXPORT_BACKOFF_MAX_SECONDS` (60) | 전송 주기·백오프 상한 |
| 토큰 | I4·I6 클라이언트 구현이 환경 변수로 받는다(이 영역은 비밀 값을 다루지 않는다) |

- `/healthz`: 프로세스가 응답하면 200. `/readyz`: 세션 DB 열림, outbox DB 열림, 활성 릴리스 적재됨이면 200, 아니면 503과 사유. 수집(I6) 도달 여부는 준비 조건이 아니다(outbox가 분리하므로).
- 로그는 표준 출력(JSON 한 줄). 발화·모델 출력 원문과 `TraceEnvelope` 내용을 로그에 남기지 않는다.
- 이미지: `Dockerfile`, 비root(uid 10001), 라벨 `io.openit.contracts.boundary`, `io.openit.survey-schema`, `io.openit.turn-engine`, `io.openit.model-runtime`, 커밋. 의존은 `git archive <태그>`로 만든 wheel로 설치한다(`scripts/build_wheels.sh`).

## 9. 문진 스키마 패키지

`schema/`에 `pyproject.toml`, `setup.py`, `survey_schema/`를 둔다. 배포 이름 `openit-survey-schema`, import 이름 `survey_schema`. 설치본은 `survey_schema/v<정수 버전>/{schemas,CONTRACT_VERSION,COMPONENTS.yaml}`을 담고, 원본은 `schema/`에 하나만 있다(빌드 때 복사). API: `load_schema(name, version=1)`, `schema_dir(version=1)`(= `<계약명>.json`이 있는 디렉터리, model-runtime `load(schema_dir=...)`에 그대로 넘긴다), `contract_version_dir`, `available_versions`, `current_version`, `list_schemas`.
설치: `pip install "openit-survey-schema @ git+ssh://git@github.com/Sdabin1209/openit_survey-service.git@<태그>#subdirectory=schema"`. 태그는 헤드가 붙인다. 새 정수 버전 N을 추가할 때는 `schema/vN/`(schemas, CONTRACT_VERSION)을 만들면 같은 wheel에 함께 들어간다. `CONTRACT_VERSION`과 정수 버전은 이 작업에서 바꾸지 않았다.

## 10. 골든 예시와의 대조

방법은 `tests/golden_replay.py`, `tests/test_golden.py`, `tests/golden_known.json`에 있다. 결과: 226 케이스, 처리한 턴 234(형식·표현 예시 13턴은 스키마 검증만), 비교한 쌍 1220(OrchestratorInput, OrchestratorOutput, FrontendInput, 세션 저장소의 처리 시작·최종 상태, StaffConsoleInput, DispatchInput/Output) 중 1215 일치, 5 불일치(모두 (c) 예시 오류). 골든 표본 1704개는 모두 자기 계약 스키마를 통과한다. 요약: 골든에는 문항 세트와 모델·엔진 입력이 없고(오케스트레이터 계약만 있다) 로그 항목 안에 컴포넌트 **출력**이 있다. 그래서 컴포넌트 출력을 재생하는 엔진·모델 대역으로 Orchestrator 자체(입력 조립, 처리 표, 저장 반영, 진행 기록, 로그, 실패 처리, 직원 알림, 결과 전달)를 비교한다. 실제 turn-engine을 골든 입력으로 돌리는 비교는 문항 세트가 없어 못 한다(CR-002는 원본에 없어 해결할 수 없다고 닫혔다). 입력 조립은 골든이 아니라 bindings.yaml과 대조한다(3.1).

## 11. O06 대기 목록

- 공개 API 4동작(세션 시작, 발화 전달, 세션 종료, 상태 확인)의 전송 방식·인증·오류 응답·타임아웃.
- `turn_id`를 누가 만드는지(지금은 호출자가 준다).
- `DELIVER_FRONT` 실패의 뜻(어떤 전달이 실패한 것인가).
- 개입 결정(`InterventionDecision`)을 서비스가 어떻게 받는지(직원 콘솔 → 서비스 경로).
- 문진 대상 참조와 개인정보 보존.
- 상태 확인 동작이 돌려줄 내용.

## 12. 수용 기준과 시험의 대응

| 기준 (brief §4) | 확인하는 시험 |
|---|---|
| 게이트가 승인한 묶음만 적재, 승인 기록 없는 묶음 거부 | `test_release_loader.py::test_unapproved_or_tampered_bundle_rejected`, `test_missing_decision_rejected` |
| 같은 세션 ID·턴 ID 재전송이 같은 결과(이중 저장 없음) | `test_orchestrator.py::test_same_turn_id_returns_same_result_without_second_write`, `test_conversation_on_sqlite_survives_restart` |
| 턴 처리 중 다른 영역 호출 없음 | `test_release_loader.py::test_registry_never_calls_the_reader_during_a_turn` |
| 모든 턴이 TurnTrace를 남기고 봉투에 릴리스 ID·계약 버전 | `test_orchestrator.py::test_trace_is_schema_valid_and_complete`, `test_trace_export.py::test_envelope_carries_release_and_contract_version` |
| 모델은 해석과 문장만, 판정·저장 결정은 코드 | 구조: Orchestrator는 판단하지 않고 `Engine`(turn-engine)을 부른다. `effects.py`는 판단 없이 효과만 적용(`test_effects.py`) |
| 새 릴리스는 새 세션부터 | `test_orchestrator.py::test_new_release_applies_to_new_sessions_only`, `test_release_loader.py::test_pointer_change_switches_active_new_sessions_only` |
| 지원하지 않는 계약 버전은 거부하고 이전 릴리스 유지 | `test_release_loader.py::test_unsupported_contract_version_is_rejected_and_previous_kept` |
| Trace는 영속 버퍼, UNAVAILABLE일 때만 재시도, 최소 1회 | `test_trace_export.py` 전체 |
| 컨테이너(D22) | `scripts/smoke.sh` |

## 13. 구현하지 않은 것 (정직하게)

- 실제 추론 백엔드(GPU)와 별도 추론 컨테이너: model-runtime에 실제 백엔드가 없다(§7).
- 직원 콘솔, 결과 전달(Result Dispatcher), 프론트 전달의 실제 연동: 접점 Protocol만 있고 기본값은 "설정되지 않음"이라 실패로 기록된다. 팩토리(`SURVEY_STAFF_CONSOLE`, `SURVEY_RESULT_DISPATCHER`)로 꽂는다.
- 공개 API(I10): O06 대기.
- 실제 release-gate(I4)·ops-feedback(I6) SDK와의 통합: 배포된 SDK가 없어 `boundary_contracts.sdk`의 Protocol을 따르는 시험용 대역(`tests/fakes.py`)으로만 시험했다. 팩토리 설정(`SURVEY_RELEASE_READER`, `SURVEY_TRACE_INGEST`)으로 실제 구현을 꽂는다.
- 세션 문서 암호화, 보존 기간, 다중 프로세스 부하 시험.

## 14. LangGraph 오케스트레이션 (D62)

사용자 지시로 한 턴의 오케스트레이션을 LangGraph `StateGraph`로 옮긴다. **동작은 바꾸지 않는다**: 기존 시험(228+), 골든 대조, 스모크가 회귀 기준이다. turn-engine은 수정하지 않고 노드가 그대로 호출한다(순수 라이브러리).

### 14.1 그래프 (노드와 조건부 간선)

```
                         +--> fail_no_session ------------------------------------------+
                         |    (조회 실패, 세션 정보 생성 실패, 활성 릴리스 없음)            |
 START -> load_session --+--> col_none ----+                                              |
          (OrchestratorInput  col_progress -+--(route 없음 = 처리 안 함)--> ignore --------+--> END
           조립)              col_paused ---+                                              |
                              col_ended ----+--(route 있음)--> bind_release --+            |
                                                                              |            |
        bind_release: 릴리스 결정, 세션 정보 생성 ---(실패)--> finalize --------+            |
              |  (발화: full)                  (plan만)                                      |
              v                                  |                                          |
   parse -> normalize -> judge -> write_record -> plan -> commit --(실패)--> finalize -------+
     |          |          |          |            |
     +----------+----------+----------+------------+--(단계 실패)--> finalize
                                                    commit --(확정)--> deliver_staff
                                                      deliver_staff --(flow=END)--> dispatch_result --> generate
                                                      deliver_staff --(그 밖)-------------------------> generate
                                                      generate -> finalize -> END
```

- **처리 표 9-1의 대응**: 열(세션 상태) = `load_session` 뒤의 조건부 간선(`col_none` / `col_progress` / `col_paused` / `col_ended`), 행(이벤트 종류) = 각 열 노드 안의 표 한 열(`Route`: 턴 종류, 세션 생성 여부, 전체 흐름 여부, 처리 안 함일 때 로그 여부). 열 노드 뒤의 간선이 `route 없음 -> ignore`, `route 있음 -> bind_release`다. "전체 흐름"은 `bind_release` 뒤의 간선(`parse` 또는 `plan`), "Plan만"은 `plan`으로 바로 간다.
- **단계별 실패**: `parse`~`commit` 노드는 예외를 잡아 `failed`에 `BEFORE_COMMIT/<단계>`를 더하고, 다음 간선이 `failed`가 비어 있지 않으면 `finalize`로 보낸다(규칙 4: 앞 단계로 돌아가지 않고, 쓰기 확정이 실패하면 확정 뒤의 일은 하지 않는다). 확정 뒤 노드(`deliver_staff`, `dispatch_result`, `generate`)는 서로의 성패와 무관하게 이어진다(`AFTER_COMMIT` 실패를 더할 뿐).
- **LangGraph가 분기하는 것**: 처리 표, 흐름 종료(`flow == END`일 때 결과 전달), 턴 실패, 그리고 규칙 문서 9-1 2항이 말하는 대로 판정 상태·행동 종류로는 분기하지 않는다.

### 14.2 상태 (직렬화 가능)

`TurnState`(TypedDict)의 값은 모두 JSON으로 직렬화되는 dict·list·문자열·수·bool·None이다. 모델 세션, 저장소, 엔진 같은 **자원은 상태에 두지 않고** `Orchestrator`가 가진다(릴리스 객체만 호출마다 `config["configurable"]["ctx"]`로 노드에 건넨다).

| 필드 | 뜻 |
|---|---|
| `session_id`, `turn_id`, `input_hash`, `message`, `decision`, `input_kind`, `in_kind`, `utterance` | 입력 |
| `loaded`, `found`, `status` | 턴 맨 앞 조회 결과 |
| `route` | 처리 표의 한 칸 `{turn_kind, create, full, keep_log}` |
| `release_id`, `state_doc`, `revision`, `created` | 릴리스 결정, 턴 시작 문서, 낙관적 revision, 세션 정보 생성 결과 |
| `parser_out`, `norm_out`, `judge_out`, `writer_out`, `records_after`, `plan_out` | 컴포넌트 출력과 저장 결정 반영 뒤 기록(timing 0-2) |
| `committed`, `commit_unknown`, `post_doc` | 쓰기 확정 결과와 확정 뒤 문서 |
| `failed`(더하기 reducer), `staff_status`, `staff_delivery`, `response_text`, `signal`, `intervention_result`, `dispatch_called`, `console`, `console_delivered`, `console_calls`(더하기) | 확정 뒤의 일과 실패 신호 |
| `trace`(병합 reducer) | TurnTrace 재료. 노드가 채운 칸만 병합 |
| `fatal`, `orchestrator_output`, `frontend_input`, `console_input`, `out_release_id`, `cacheable` | 종료 노드의 결과 |

### 14.3 그래프 밖에서 유지하는 것

- **세션별 직렬화(락), turn_id 멱등(저장된 결과 재생), 입력 검증**: `handle_turn`이 그래프를 부르기 전·후에 한다.
- **한 턴의 쓰기 확정**: `commit` 노드가 저장소의 한 번의 `commit`(SQLite 한 트랜잭션)을 부른다. LangGraph의 체크포인터·`Send`·병렬 가지를 쓰지 않는다. **체크포인터를 쓰지 않고** 컴파일한다(`compile()`에 checkpointer 없음). 세션 저장소는 이 영역의 Session Store 하나다.
- **Trace outbox**: 그래프가 끝난 뒤 `handle_turn`이 outbox에 넣는다.
- **출력 보호(D36)**: 모델 세션(model-runtime, 또는 추론 서버) 안에서 일어난다. 그래프는 보호가 끝난 출력을 받는다.

### 14.4 위험과 결정

| 위험·결정 | 대응 |
|---|---|
| 동작이 바뀐다 | 기존 시험 전체와 골든 대조를 그대로 통과해야 한다. 노드는 기존 함수 본문을 옮긴 것이고 로그·Trace의 필드 순서와 값이 같다 |
| 의존이 크게 늘어난다(langgraph가 langchain-core, pydantic 등을 끌어온다) | 버전을 잠금 파일에 고정하고 이미지에 반영한다. 라벨 `io.openit.langgraph`를 더해 버전을 기록한다. 이 라벨은 공통 규칙의 6개 키 밖의 **추가** 키다(CR-011) |
| **외부 전송(추적)**: LangChain 생태계는 환경 변수로 LangSmith 추적을 켤 수 있다. 문진 발화가 밖으로 나가면 안 된다 | 이미지에 `LANGSMITH_TRACING=false`, `LANGCHAIN_TRACING_V2=false`를 고정하고 API 키를 두지 않는다. 시험이 그래프 실행 중 외부 연결이 없음을 확인한다(소켓 차단) |
| 스택 깊이·성능 | 노드 약 20개, 턴당 한 번 호출. 시험으로 지연이 늘지 않음을 확인한다 |
| 상태가 직렬화되어야 한다 | 시험이 최종 상태를 `json.dumps`로 확인한다 |
| 처리 표를 코드 표와 그래프 간선 둘로 두면 어긋난다 | 표는 열(세션 상태)마다 한 곳(`COLUMNS`)에만 두고 `route()`는 그 표를 읽는 얇은 함수다. 기존 표 시험이 그대로 걸린다 |

### 14.5 구현 (survey_service/graph.py)

- 노드는 `TurnGraph`의 `n_<이름>` 메서드이고 이전 `_Turn` 기반 함수 본문을 옮긴 것이다(필드 순서·값·직원 콘솔 호출 순서 불변). 열 표는 `COLUMNS`, `route()`는 그 표를 읽는 얇은 함수다. 상태는 `TurnState`, 시작 상태는 `initial_state()`, 호출마다 건네는 자원(릴리스, 문항 세트 보조 객체)은 `TurnCtx`다. `Orchestrator.handle_turn`은 락, 검증, 멱등, 결과 저장, Trace outbox를 그래프 밖에서 한다.
- 회귀: 기존 시험 전체와 골든 대조(1220쌍 중 1215 일치, 5건 예시 오류)가 그대로 통과한다. `tests/test_graph.py`가 간선 구성, 되돌아가는 간선이 없음, 체크포인터 없음, 표와 `route()`의 일치, 모든 경로의 최종 상태가 JSON 직렬화 가능함, 턴 처리 중 네트워크 연결 없음, 추적 환경 변수가 꺼져 있음, 잠금 파일의 langgraph 고정과 설치본 일치를 확인한다.
- 의존: `langgraph` 1.2.15(잠금 파일에 정확히 고정, `pyproject.toml`은 `>=1.2,<1.3`). langchain-core, pydantic, langsmith 등 43개 패키지가 잠금에 들어온다(이전 6개). 이미지 라벨 `io.openit.langgraph`(CR-011), 이미지 환경에 `LANGSMITH_TRACING=false`, `LANGCHAIN_TRACING_V2=false`.
