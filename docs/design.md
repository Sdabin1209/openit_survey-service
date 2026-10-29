# survey-service 영역 설계서

> 이 문서는 D74 구조(한 턴의 흐름 16절, 문항 순서·건너뛰기 표 17절, 표 찾기와 B 입력 18절, Trace 키·오류·위험 신호 19절)를 서술한다. v1 시절 절은 이력 문서로 옮겼고 이 문서에서 참조하지 않는다.

표기: **[확인]** 계약·브리프·규칙 문서에서 읽은 것, **[결정]** 이 영역이 정한 내부 설계(경계에 닿지 않음), **[제안]** 규칙 문서에 없어 이 문서가 고른 해석(바꿀 수 있음, 필요하면 변경 요청), **[미정]** 정해지지 않은 것, **[O06 대기]** 공개 API가 정해져야 하는 것.

## 1. 범위

- 하는 일 [확인: brief §1]: 한 턴을 처리하는 Orchestrator, Session Store, 릴리스 로더, Trace 내보내기, 추론 서버, 문진 스키마 패키지(`schema/`).
- 하지 않는 일 [확인]: 판단 로직(모델과 서버), 모델 로딩·프롬프트·디코딩(model-runtime), 문항·모델 승인(게이트), Frontend 화면.
- **공개 API(I10)** [확인: D55, `boundary-contracts/PUBLIC_API.md`]: 키오스크 프론트가 부르는 HTTP/JSON. `POST /v1/sessions`(시작. 본문의 `session_id`를 생략하면 서버가 `secrets.token_urlsafe(32)`로 발급해 응답 최상위 `session_id`로 돌려준다. 프론트가 정해 보내는 방식은 2.x에서만 허용하는 폐기 예정 방식이라 수용하되 경고를 남긴다. 스키마: `FrontendMessage`·`FrontendInput`), `POST /v1/sessions/{id}/turns`(발화, `turn_id` 멱등), `POST /v1/sessions/{id}/end`(종료), `GET /healthz`, `/readyz`. Bearer 토큰(환경 변수의 이름으로 지정, `SURVEY_API_TOKEN_ENV`). 구현은 `survey_service/api.py`가 기존 `handle_turn`(엔진·기록·직원 호출·결과 전달·Trace)에 그대로 연결하고, 문서가 정하지 않은 점의 해석은 CR-009에 적었다. 요청은 `FrontendMessage`로 검증하고 응답은 `FrontendInput`이다. 상태 코드: 400/401/404/409/429/503. 세션 유휴 만료(기본 300초, `SURVEY_SESSION_IDLE_SECONDS`)는 이탈 종료로 닫는다. 개발용 CLI(`python -m survey_service.cli`)는 그대로 있다. 직원 개입은 `POST /v1/sessions/{id}/intervention`(역할 `staff` 토큰, 키오스크 토큰은 403, 위험으로 멈춘 세션만, 아니면 409, `turn_id` 멱등)이 `InterventionDecision` 처리에 연결된다 [확인: D58, v1.3.1].

## 2. 컴포넌트 지도

```
              +-------------------- survey_service (프로세스) ---------------------+
 handle_turn  | Orchestrator(graph.py) --- 서비스 고유 검사(check_slots, check_values). 의미 규칙은 서버(I9 flow)가 보증
 (라이브러리) |      |      \-- Inference(model-runtime ModelSession: extract/respond)
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
| `survey_service.orchestrator` | 턴 입구(`graph.py`의 `route`, 16절), 턴 순서, 쓰기 확정, 확정 뒤의 일, 실패 신호, 로그, 멱등 |
| `survey_service.store.effects` | 저장소 반영 효과를 순수 함수로. 판단 없음 |
| `survey_service.store.sqlite` / `memory` | Session Store 구현 두 개. 같은 적합성 시험을 통과 |
| `survey_service.release` | 활성 포인터 폴링, 번들 적재, 거부, 이전 릴리스 유지, 릴리스 레지스트리 |
| `survey_service.inference` | 릴리스마다 ModelSession을 만들고 보관(추론 서버), 백엔드 선택 |
| `survey_service.trace` | TurnTrace 조립, 봉투, 영속 outbox, 전송 스레드 |
| `survey_service.service` | 조립, 설정(환경 변수), `/healthz` `/readyz` |
| `schema/survey_schema` | 문진 스키마 패키지(별도 배포 단위) |

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
  4. `manifest.action_rules_version`이 설치된 계약의 규칙 판과 다르거나 `model_runtime_version`의 **MAJOR.MINOR가 설치된 값과 다르면 적재를 거부**하고 이전 릴리스를 유지한다(`VERSION_MISMATCH`, 사유는 `/readyz`의 `release_loader.rejections`). **PATCH만 다르면 경고만 하고 적재한다**. 게이트가 승인한 동작과 서비스 동작이 달라지지 않게 하기 위해서다 [확인: 헤드 결정 D45, CR-005]. 해석할 수 없는 버전 문자열도 거부한다.
  5. `model_runtime.load`가 거부하면(`ServingConfigMismatch`, `UnsupportedServingConfig`, `ContractVersionMismatch`, 백엔드 없음) 적재 실패로 보고 이전 릴리스를 유지한다.
- 성공하면 레지스트리에 넣고 활성 릴리스를 바꾼다. **새 세션만** 새 릴리스에 묶인다. 진행 중인 세션은 자기 릴리스로 끝까지 처리한다(세션은 `release_id`를 가진다).
- **턴을 처리하는 동안 이웃(I4)을 부르지 않는다** [확인: brief §4]. 그래서 서비스를 다시 띄운 뒤 진행 중 세션이 가리키는 릴리스는 시작할 때와 폴링 주기마다 턴 밖에서 `ensure_loaded`로 미리 적재한다. 적재하지 못한 릴리스는 `/readyz`의 `release_loader.unavailable_releases`에 나오고, 그 세션의 턴은 첫 단계 실패(`BEFORE_COMMIT/LoRA-A`)로 처리한다 [제안]. 새 세션을 받는 능력(준비 여부)에는 영향이 없다.
- 열린 세션이 없고 활성이 아닌 릴리스는 최근 `SURVEY_KEEP_RELEASES`개를 남기고 내린다.
- **일시적 실패와 결정적 거부의 구분**: 추론 서버가 아직 뜨는 중(연결 실패, 502·503·504, 읽기 타임아웃, 적재 신호 뒤 LOADING 대기 시간 초과)이나 I4 불능은 포인터를 거부로 확정하지 않고 같은 포인터를 백오프(`SURVEY_RELEASE_RETRY_BASE_SECONDS` 5초 시작, `..._MAX_SECONDS` 60초, 상한 `..._GIVE_UP_SECONDS` 기본 없음, 넘으면 `RETRY_EXHAUSTED`로 거부)로 계속 다시 시도한다. 재시도 중 포인터가 새 버전으로 바뀌면 새 포인터를 먼저 본다. 412·422·404·403 등 결정적 거부와 승인·해시·버전 검사 실패는 지금처럼 영구 거부+이전 릴리스 유지(D46)다. 재시도 상태는 `/readyz`의 `release_loader.retrying`(포인터 버전, 시도 횟수, 마지막·다음 시도 시각, 마지막 오류)이고, 활성 릴리스가 없으면 `problems`에 `release_loading`이 더해진다(거부와 구분). 첫 적재는 폴링 스레드에서 하고 `start()`는 `SURVEY_STARTUP_WAIT_SECONDS`(5초)까지만 기다린다: GPU 모델 적재에 수 분이 걸려도 서비스는 열려 있다.
- 상태: 활성 릴리스 ID, 마지막 포인터 버전, 거부 목록(사유 코드, 포인터 버전). `/readyz`와 런북에서 본다.
- 첫 활성 포인터가 없으면(`None`) 서비스는 살아 있으나 준비되지 않았다(`/readyz` 503).

## 6. Trace 내보내기

- 턴마다 `TurnTrace` 하나. 조립 규칙 [확인: trace_contract]: 각 경계는 해당 계약의 전체 payload 또는 null. null은 미확보(예: 처리 안 함 턴의 컴포넌트 입출력). `session_before`/`session_after`는 턴 시작 문서·턴 끝 문서, `persistence_*`는 조회·커밋 요청과 결과.
- `TraceEnvelope`: `pipeline_schema_version`(boundary-contracts가 정한 값), `release_id`(세션이 묶인 릴리스, 세션이 없으면 활성 릴리스), `runtime_contract_version`, `session_id`, `turn_id`, `sent_at`, `trace`.
- **외부 ID 변환** [확인: CONVENTIONS 10절, CR-007]: 세션 ID·턴 ID가 `Id` 형식(`[A-Za-z0-9][A-Za-z0-9._:-]*`, 128자)이면 그대로, 아니면 `x-` + 원본 UTF-8 SHA-256 앞 32자(소문자 16진)를 봉투에 넣는다. 결정적이라 outbox의 (세션, 턴) 유일성과 수집의 멱등이 유지되고, 원본은 `trace` 본문에 남는다. 서비스 안의 세션·턴 식별(저장소, 멱등)은 원본 ID를 쓴다. 공개 API(O06)가 `Id` 형식으로 발급하기로 정해지면 변환은 쓰이지 않는다.
- **outbox** [결정]: 세션과 **같은 DB**의 표(SQLite도 세션 DB 파일. `SURVEY_OUTBOX_DB`는 더 쓰지 않는다). `(session_id, turn_id)` 유일. 세션 commit 트랜잭션이 예약 행을 쓰고 턴이 끝나면 한 트랜잭션으로 완성본으로 바꾼다(트랜잭셔널 outbox, ADR-0023). 전송 스레드가 별도로 `send`를 부른다.
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
| `SURVEY_OUTBOX_DB` (/data/outbox.db) | 더 쓰지 않는다. Trace outbox는 세션 DB에 있다(ADR-0023) |
| `SURVEY_RELEASE_READER` | `http`(기본: `boundary_contracts.clients.HttpReleaseReader`, D49) 또는 팩토리 `모듈:함수`. `SURVEY_RELEASE_READER_URL`, 토큰이 든 환경 변수의 이름 `SURVEY_RELEASE_READER_TOKEN_ENV` |
| `SURVEY_TRACE_INGEST` | `http`(`HttpTraceIngest`) 또는 팩토리. `SURVEY_TRACE_INGEST_URL`, `SURVEY_TRACE_INGEST_TOKEN_ENV` |
| `SURVEY_MODEL_BACKEND` | `fake` 등 |
| `SURVEY_RELEASE_POLL_SECONDS` (5) | 포인터 폴링 주기 |
| `SURVEY_KEEP_RELEASES` (2) | 내리지 않고 남길 비활성 릴리스 수 |
| `SURVEY_EXPORT_INTERVAL_SECONDS` (1), `SURVEY_EXPORT_BACKOFF_MAX_SECONDS` (60) | 전송 주기·백오프 상한 |
| 토큰 | I4·I6 클라이언트 구현이 환경 변수로 받는다(이 영역은 비밀 값을 다루지 않는다) |

- `/healthz`: 프로세스가 응답하면 200. `/readyz`: 세션 DB 열림, outbox DB 열림, 활성 릴리스 적재됨이면 200, 아니면 503과 사유. 수집(I6) 도달 여부는 준비 조건이 아니다(outbox가 분리하므로).
- 로그는 표준 출력(JSON 한 줄). 발화·모델 출력 원문과 `TraceEnvelope` 내용을 로그에 남기지 않는다.
- 이미지: `Dockerfile`, 비root(uid 10001), 라벨 `io.openit.contracts.boundary`, `io.openit.survey-schema`, `io.openit.model-runtime`, 커밋. 이웃 의존은 영역 빌드 진입점 `scripts/build-image.sh`가 요청의 소스 트리로 설치한다(ADR-0037).

## 9. 문진 스키마 패키지

`schema/`에 `pyproject.toml`, `setup.py`, `survey_schema/`를 둔다. 배포 이름 `openit-survey-schema`, import 이름 `survey_schema`. 설치본은 `survey_schema/v<정수 버전>/{schemas,CONTRACT_VERSION,COMPONENTS.yaml}`을 담고, 원본은 `schema/`에 하나만 있다(빌드 때 복사). API: `load_schema(name, version=1)`, `schema_dir(version=1)`(= `<계약명>.json`이 있는 디렉터리, model-runtime `load(schema_dir=...)`에 그대로 넘긴다), `contract_version_dir`, `available_versions`, `current_version`, `list_schemas`.
설치: `pip install "openit-survey-schema @ git+ssh://git@github.com/Sdabin1209/openit_survey-service.git@<태그>#subdirectory=schema"`. 태그는 헤드가 붙인다. 새 정수 버전 N을 추가할 때는 `schema/vN/`(schemas, CONTRACT_VERSION)을 만들면 같은 wheel에 함께 들어간다. `CONTRACT_VERSION`과 정수 버전은 이 작업에서 바꾸지 않았다.

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

## 14. LangGraph 오케스트레이션 (D62) [노드 구성은 16절로 대체. 아래 14.3의 "그래프 밖에서 유지하는 것", 14.4의 위험·결정은 그대로 유효]

사용자 지시로 한 턴의 오케스트레이션을 LangGraph `StateGraph`로 옮긴다. **동작은 바꾸지 않는다**: 기존 시험과 스모크가 회귀 기준이다. turn-engine은 수정하지 않고 노드가 그대로 호출한다(순수 라이브러리).

### 14.1 그래프와 상태

노드 구성과 상태는 16절이 정한다.

### 14.3 그래프 밖에서 유지하는 것

- **세션별 직렬화(락), turn_id 멱등(저장된 결과 재생), 입력 검증**: `handle_turn`이 그래프를 부르기 전·후에 한다.
- **한 턴의 쓰기 확정**: `commit` 노드가 저장소의 한 번의 `commit`(SQLite 한 트랜잭션)을 부른다. LangGraph의 체크포인터·`Send`·병렬 가지를 쓰지 않는다. **체크포인터를 쓰지 않고** 컴파일한다(`compile()`에 checkpointer 없음). 세션 저장소는 이 영역의 Session Store 하나다.
- **Trace outbox**: 그래프가 끝난 뒤 `handle_turn`이 outbox에 넣는다.
- **출력 보호(D36)**: 모델 세션(model-runtime, 또는 추론 서버) 안에서 일어난다. 그래프는 보호가 끝난 출력을 받는다.

### 14.4 위험과 결정

| 위험·결정 | 대응 |
|---|---|
| 동작이 바뀐다 | 기존 시험 전체를 그대로 통과해야 한다. 노드는 기존 함수 본문을 옮긴 것이고 로그·Trace의 필드 순서와 값이 같다 |
| 의존이 크게 늘어난다(langgraph가 langchain-core, pydantic 등을 끌어온다) | 버전을 잠금 파일에 고정하고 이미지에 반영한다. 라벨 `io.openit.langgraph`를 더해 버전을 기록한다. 이 라벨은 공통 규칙의 6개 키 밖의 **추가** 키다(CR-011) |
| **외부 전송(추적)**: LangChain 생태계는 환경 변수로 LangSmith 추적을 켤 수 있다. 문진 발화가 밖으로 나가면 안 된다 | 이미지에 `LANGSMITH_TRACING=false`, `LANGCHAIN_TRACING_V2=false`를 고정하고 API 키를 두지 않는다. 시험이 그래프 실행 중 외부 연결이 없음을 확인한다(소켓 차단) |
| 스택 깊이·성능 | 노드 약 20개, 턴당 한 번 호출. 시험으로 지연이 늘지 않음을 확인한다 |
| 상태가 직렬화되어야 한다 | 시험이 최종 상태를 `json.dumps`로 확인한다 |
| 처리 표를 코드 표와 그래프 간선 둘로 두면 어긋난다 | 표는 열(세션 상태)마다 한 곳(`COLUMNS`)에만 두고 `route()`는 그 표를 읽는 얇은 함수다. 기존 표 시험이 그대로 걸린다 |

### 14.5 구현

구현은 `survey_service/graph.py`. 노드 구성은 16절을 본다.

## 15. 직원 알림과 공개 API 보완 (D64, D66)

계약은 `boundary-contracts/PUBLIC_API.md`(버전은 설치된 계약 패키지 기준). 구현은 `survey_service/alerts.py`(저장·전달), `api.py`(HTTP 동작), `service.py`(SSE 스트림).

- **알림 저장** [확인]: 직원 알림이 나면(`StaffAlert.kind` RISK, CALL_STAFF, USER_CALLED_STAFF → 알림 종류 `RISK_STOP`, `CALL_STAFF`, `USER_CALLED_STAFF`) SQLite(`SURVEY_ALERT_DB`, 비우면 세션 DB 옆 `alerts.db`)에 영속 저장한다. 알림 행과 이벤트 행은 한 트랜잭션이다. 세션 저장소와는 다른 파일이라 세션 쓰기 확정과 한 트랜잭션은 아니다: 쓰기 확정 뒤 알림 저장 전에 프로세스가 죽으면 알림이 없고, 같은 `turn_id` 재시도는 `DELIVERY_FAILED` 신호를 돌려준다(복구 경로) [제안: 단일 파일로 합치지 않은 이유는 알림을 세션 저장소와 독립적으로 보존·조회하기 위해서다].
- **전달 판정** [확인]: 알림이 저장되고 전달 경로가 하나 이상 살아 있으면 `DELIVERED`(SSE 연결 수 > 0 이거나 웹 푸시 서비스가 접수), 아니면 `AlertStaffConsole`이 `DeliveryFailed`를 던져 Orchestrator가 `DELIVERY_FAILED`로 기록한다(응답 단계는 그대로). 저장 실패도 전달 실패다. **한계**: SSE 연결이 살아 있는지는 서버가 쓰기에 실패해야 안다. 클라이언트가 소리 없이 사라지면(프록시가 연결을 닫지 않는 경우 포함) 다음 `: ping`(기본 15초, `SURVEY_SSE_HEARTBEAT_SECONDS`) 전까지 DELIVERED로 잘못 볼 수 있다. 직원 화면이 연결 확인을 하는 운영 절차가 필요하다 [미정].
- **본문** [확인]: 알림, SSE, 푸시 본문에는 세션 ID와 종류만 있다(환자 발화·개인 정보 없음, 시험이 확인).
- **SSE**: `GET /v1/staff/alerts/stream`. 이벤트 이름 `alert`(새 알림), `resolved`(해결), `id:`는 이벤트 순번(알림과 해결 이벤트가 각각 하나씩 증가, 영속), `data:`는 `StaffAlert` JSON, 15초마다 `: ping`. `Last-Event-ID` 뒤의 이벤트부터 이어서 보낸다(서비스를 다시 띄워도 된다). 이벤트는 최근 1만 개를 남긴다(`trim_events`).
- **웹 푸시**: `pywebpush`(잠금 파일에 고정). VAPID 비밀키는 환경 변수 `SURVEY_VAPID_PRIVATE_KEY`(이름은 `SURVEY_VAPID_PRIVATE_KEY_ENV`로 바꿀 수 있다, PEM 또는 base64url DER)와 `SURVEY_VAPID_SUBJECT`(mailto: 또는 https://)로만 받는다. **키가 없으면 푸시 없이 SSE만** 동작하고 `GET /v1/staff/push-config`는 `{"vapid_public_key": null}`이다. 키가 있으면 공개키(applicationServerKey)를 낸다. 키 형식이 잘못되면 시작하지 않는다. 푸시는 구독마다 병렬로 보내고 `SURVEY_PUSH_TIMEOUT_SECONDS`(3초)까지만 기다린다. 만료된 구독(404, 410)은 지운다.
- **구독**: `POST /v1/staff/push-subscriptions`(201, 같은 endpoint는 갱신, https만, 한도 `SURVEY_MAX_PUSH_SUBSCRIPTIONS`는 409), 해지는 `POST /v1/staff/push-subscriptions/unsubscribe` 본문 `{"endpoint"}`(204, 없는 endpoint도 멱등 204; URL에 푸시 권한 값을 싣지 않으려고 본문). `DELETE ...?endpoint=`(200)는 2.x에서만 허용하는 폐기 예정 방식이라 남기되 경고를 로그에 남긴다(URL은 로그에 없다).
- **해결**: 개입 결정이 성공(`intervention_result APPLIED`)하면 그 세션의 열린 알림을 `RESOLVED`(`resolved_by_decision` RESUME/END)로 하고 `resolved` 이벤트를 낸다. 같은 `turn_id` 재전송에서도 다시 해 본다(멱등). 키오스크나 유휴 만료로 세션이 끝나면 결정 없이 해결한다.
- **세션 상태**(키오스크, `GET /v1/sessions/{id}/state`): `ACTIVE | PAUSED_RISK | ENDED`와 `resume_text`. 직원이 재개하면 재개 턴의 안내 문장이 다음 발화 턴이 접수되기 전까지 `resume_text`로 오고 그 뒤 null이다. 대화 로그에서 계산하므로 재시작해도 같다. 폴링 빈도 상한(`SURVEY_STATE_POLL_MIN_INTERVAL_SECONDS`, 기본 0.5초, 세션마다)을 넘으면 429.
- **오류 표** [확인: D66]: 400 INVALID_RECORD, 401·403 PERMISSION_DENIED, 404 NOT_FOUND, 409 CONFLICT, 429·503 UNAVAILABLE. 429·503에는 `Retry-After`(초, 정수, `SURVEY_RETRY_AFTER_SECONDS` 기본 1)가 붙는다.
- **turn_id 봉투**: 본문에서 `turn_id`를 떼어 낸 나머지를 `FrontendMessage`/`InterventionDecision`으로 검증한다(문진 스키마는 바꾸지 않았다). 시작·종료에서는 선택 필드이고 없으면 고정 ID(`start`, `end`)다.
- **유휴 만료**: 위험 정지(PAUSED_RISK) 세션은 만료하지 않는다(`list_idle`이 진행 중 세션만 낸다, 시험이 확인).
- 직원 접점은 교체 가능하다: 기본은 `AlertStaffConsole`(StaffConsole Protocol 구현), `SURVEY_STAFF_CONSOLE`로 다른 구현을 꽂으면 그것을 쓴다(알림 조회·SSE API는 알림 저장소를 그대로 쓰므로 이 경우 알림은 저장되지 않는다).


## 16. D74 구조: 한 턴의 흐름 (작성 시점 2026-10-27 기록. 현행 버전은 `requirements.txt`)

[확인] 정규화·판정·기록·Plan 코드는 서비스에 없다. 모델(LoRA-A)이 값 추출·정규화·절차(action)·다음 문항(next_item)을 한 번에 내고, 서비스는 **검증·저장·전달·표 찾기**만 한다. 판단은 하지 않는다(모델의 출력을 `validate_extraction`이 확인할 뿐이다).

```
 load_session -> col_* -> (ignore | bind_release)
 bind_release -> extract -> validate_extraction -> plan_writes -> commit -> deliver_staff -> [dispatch_result] -> respond -> finalize
                   ^  |            |  (위반)                                                       
                   +--+------------+  같은 요청 1회 재시도(D75)          
 bind_release -> plan_other -> commit -> ... (시작, 직원 재개·종료, 이탈: A를 부르지 않는다)
```

| 노드 | 하는 일 |
|---|---|
| `extract` | `ActionExtractionInput`을 조립해(17절) 추론 서버 `extract`를 한 번 부른다. 이 노드가 다시 실행되면 같은 요청의 재시도다 |
| `validate_extraction` | 서버가 보증하는 의미 규칙은 다시 보지 않는다(ADR-0028). 서비스 고유 검사만: 저장할 칸이 문항 세트에 있는가(`UNKNOWN_SLOT`)와 값 타당성(`check_values`). 위반이 있으면 한 번 재시도, 그래도면 턴 실패 |
| `plan_writes` | `save=true`인 값만 `AnswerUpdate`로(칸에 값이 이미 있으면 정정 `NEW_REVISION`, 이력에 쌓음), 진행 갱신(`next_item`, 미저장 칸, 건너뜀, 문진 종료), 위험 입력원 호출(있을 때) |
| `plan_other` | 비발화 턴의 쓰기·응답 계획. SESSION_START는 B만(`action=START`, `next=Q1.D01`), 직원 재개는 고정 문장 RESUME, 직원 종료는 SURVEY_STOPPED, 이탈은 응답 없음 |
| `commit` | 값 저장과 진행 갱신을 한 트랜잭션으로(그대로) |
| `respond` | B 입력을 표 찾기로 조립해(18절) `respond` 호출. 고정 문장 턴은 문항 세트의 `fixed_sentences`(제안: v1 어휘의 RESUME, SURVEY_STOPPED, RISK_NOTICE 키를 그대로 씀) |

- **저장 규칙** [제안, 계약의 `save` 규칙을 따름]: ANSWERED·CORRECTION(`save=true`)만 저장한다. CONFLICT는 현재 값을 지키고 CONFIRM으로 되묻는다(A가 다음 턴에 CORRECTION으로 확정). UNKNOWN·REJECTED·MISSING·UNCLEAR는 저장하지 않는다. SKIP·HOLD는 `progress_record.skipped`에 사유(`REQUEST_SKIP`, `REFUSED`, `UNCERTAIN`)와 문항을 남기고, 나중에 그 문항의 값이 저장되면 지운다. 같은 값을 같은 정밀도로 다시 말하면 쓰지 않는다.
- **세션 문서(SessionStore)**: `progress_record`는 `current_question_id`(다음 발화를 해석할 문항, 끝나면 `END`), `open_slots`(직전 추출이 MISSING·UNCLEAR로 본 칸, 문항이 바뀌면 비움), `skipped`, `session_status`, `pause_history`다. v1의 `last_directive`, `open_requests`, `confirm_stopped`, `planner_record`, 확인 대기(pending), `value_metadata`는 없앴다. `Value`는 bool·수를 받는다. `current_question`(A 입력)은 대화 로그의 마지막 응답 문장이다(별도 칸 없음). 직전 응답이 없으면(응답 실패 등) 문항 은행의 질문 문장.
- **재시도·실패(D75)**: 503 `OUTPUT_INVALID`이거나 `validate_extraction` 위반(또는 `UNKNOWN_SLOT`)이면 같은 요청을 1회 더 보낸다. 그래도면 `turn_failure_signal`(`write_committed=NOT_COMMITTED`)이고 세션 상태는 바뀌지 않는다(대화 로그만 남는다). 그 밖의 호출 실패는 클라이언트 층에서 연결 실패만 1회 재시도하고 읽기 타임아웃은 재시도하지 않는다(`remote.py`). 입력 400은 재시도하지 않는다. B가 실패하면 쓰기는 확정된 채 `AFTER_COMMIT/LoRA-B` 실패 신호다(v1과 같다). 재시도 상한은 한 곳(`MAX_EXTRACT_CALLS = 2`)이다.
- **미정**: v1에는 문항별 물음 상한(`ask_limit`, `question_limit`, `confirm_limit`)과 연속 못 알아들음 상한(`unrecognized_streak_limit`)이 Plan 안에 있었다. v2에서는 A가 REASK·CONFIRM을 내는 한 서비스가 막지 않는다. 같은 문항에서 반복되는 되묻기를 끊는 정책은 정해진 것이 없어 넣지 않았다(헤드 결정 필요, CR-012에 함께 적음).

## 17. 문항 순서·건너뛰기 표(D76)와 A 입력 state

[확인] 소유는 이 영역이다. 문진 스키마에 `QuestionFlow` 계약(`schema/schemas/QuestionFlow.json`)과 데이터(`schema/data/question_flow.json`)를 두었다: `order`(31개 문항), `skips`(`{slot, op: "==", value, skip_item}` 7건, `Q8_1.days == 0` 같은 수 조건 포함), `reference_sources`, `slot_order`. 서비스(`survey_service/flow.py`)가 이 데이터를 계약으로 검증한 뒤 자체 `FlowTable`을 만들어 A 입력 `state` 조립에 쓰고, 추론 서버 extract 요청의 `flow`(계약 QuestionFlowTable)로 보낸다(ADR-0028). `tests/test_extraction.py::test_flow_built_from_survey_schema_is_the_contract_flow_table`가 계약 형식과 같음을 시험한다. 비교 연산은 `==`뿐이다(문진 스키마 `QuestionFlow`가 정한다). 다른 연산이 필요해지면 문진 스키마와 계약 규칙 명세의 변경이다.

문진 스키마의 **정수 버전은 올리지 않았다**(`CONTRACT_VERSION` 1 그대로). 이유: model-runtime 0.6.0이 `SUPPORTED_RUNTIME_CONTRACT_VERSION = 1`만 받는다. 대신 같은 정수 버전 안에서 v1 모델 입출력 계약 12개(`UtteranceParserInput` 등)를 지우고 `SessionStore`, `TurnTrace` 등을 바꾸고 `QuestionFlow`, `ResponseTables`를 더했다. **이것은 같은 정수 버전 안의 비호환 변경이다**(ops-feedback이 가진 TurnTrace 사본, 결과 전달의 DispatchInput). 정수 버전을 2로 올릴지(그러려면 model-runtime의 지원 값부터 바뀌어야 한다)는 헤드 결정이다.

A 입력 `state`의 규칙은 학습 데이터(train·val·test의 A 입력 5,077행 전수)에서 읽은 것이다 [확인: 묶음 순서(지금 문항 저장 값 -> 다른 문항 -> 지금 문항 미저장)는 5,077행 모두 일치. 다른 문항의 구성은 직전 두 문항과 참조 원천 안에 드는 것이 5,062행이고 나머지 15행(Q8_1이 Q7을 가짐)은 Q7_1·Q7_2를 건너뛴 직전 문항 규칙으로 설명되나 전수를 규칙으로 재현해 대조하지는 않았다. 한계는 아래]:
1. 지금 문항의 **저장된 값**(status `ANSWERED`) -> 2. **직전 문항**(문항 순서에서 바로 앞, 건너뛰기 규칙이 참인 문항은 건너뜀)과 **참조 원천 문항**의 저장된 값 -> 3. 지금 문항의 **MISSING·UNCLEAR** 칸(값 null). 각 항목은 `{"value", "status"}` 순서.
- 한 문항 안의 칸 순서: 키 문자열 정렬이 2,145개 묶음에서 맞고, Q4_1·Q5_1의 "다른 문항" 묶음 333개는 문진 스키마의 `slot_order`(status, total_years, daily_count, years_since_quit)와 같았다. 지금 문항 묶음에서 두 칸 이상인 경우는 39개뿐이고 모두 정렬 순서였다(Q4_1·Q5_1이 지금 문항일 때의 순서는 데이터가 없어 확인하지 못했다. 서비스는 같은 `slot_order`를 쓴다).
- 2의 묶음 순서는 직전 문항이 먼저, 참조 원천이 나중이 다수(약 90%)다. 나머지 약 10%는 반대 순서였고 이유를 찾지 못했다 [미확인]. 서비스는 다수 규칙을 쓴다. 참조 원천(`reference_sources`: Q5←Q4, Q5_1←Q4_1, Q6←Q5, Q9_1←Q8_1, Q9_2←Q8_2, Q10←Q9_1)도 같은 표본에서 읽은 것이다.
- 직렬화는 model-runtime의 몫이다(헤드 확인: 학습 글자는 `json.dumps(obj, ensure_ascii=False)`). 서비스는 객체를 만든다: A 입력 최상위 키는 알파벳 순(`current_question`, `question_id`, `state`, `user_utterance`), `state`는 삽입 순서 보존, B 입력은 모든 수준의 키 정렬. `tests/test_extraction.py::test_extract_input_equals_training_data_character_for_character`가 학습 표본 16건에서 서비스가 만든 A 입력을 `json.dumps`한 글자가 학습 데이터와 같음을 확인한다.
- `MISSING`·`UNCLEAR` 칸의 출처는 서비스가 기억한다(`progress_record.open_slots`): 직전 추출의 미저장 MISSING·UNCLEAR 값. 그 문항을 벗어나면 지운다. 학습 데이터에서 이 칸이 몇 턴 동안 이어지는지는 확인하지 못했다.

**val 대조(2026-10-27, model-runtime 0.6.1)** [확인: `scripts/check_serialization_against_val.py`, 학습 서버 `dataset_10_14`의 `lora_a_val.jsonl` 549행·`lora_b_val.jsonl` 560행을 표준출력으로 흘려 이 PC에서 계산, 저장하지 않음. A 행의 state에서 세션 문서를 거꾸로 만들어 서비스 코드로 다시 조립한 것이라 state 구성은 in-sample이다]:
- A: 입력 객체 549/549 같음(state 키 집합, 값 내부 키 순서 `value, status`도 549/549). state 키 **순서**와 그에 따른 `json.dumps` 및 prompt-4 user 글자는 536/549 같음. 불일치 13행은 모두 state 안 묶음 순서다: Q10 4행(val은 Q9_1 -> Q9_2, 서비스는 직전 문항 Q9_2 먼저), Q9_2 2행(val은 참조 Q8_2 먼저), Q6 4행(참조 Q5 먼저), Q6 3행(Q5_1 칸이 알파벳 순). 같은 문항에서 같은 규칙으로 맞는 행도 많아(Q10 34행, Q9_2 18행, Q6 6행) 문서에서 읽을 수 있는 기준을 찾지 못했다 [미확인, 고치지 않음: 규칙 없이 맞추면 다수 행이 깨진다].
- B: A와 짝지은 549행(짝 없는 11행은 START)의 키 집합과 값 형태 549/549 같음. START 11행도 키 집합 같음. 글자는 `next.text`를 뺀 나머지가 549/549 같고, `next.text`만 다르다(문항 은행 문장 대 데이터의 바꿔 쓴 문장, 이 시험은 시험용 문항 세트라 더 다르다. 18절 설명대로 데이터를 따르지 않는 것이 의도이고 실제 문항 세트 문장으로는 확인하지 않았다).

## 18. 표 찾기와 B 입력: 데이터셋 담당의 규칙을 옮김 (**데이터셋 담당 확인 대기, 승인 기록 미확인**)

B 입력의 문장 필드는 **데이터셋 담당의 규칙**을 따른다. 학습 서버 `~/WS/dataset/scripts/labels.py`(2026-10-14)의 `topic`, `short_topic`, `value_text`, `_group_texts`, `ask_text`(`_ask_missing`, `_ask_option`, `_ask_number`), `_known_texts`, `hold_text`, `previous_text`, `_conflict_rows`, `b_input`이 학습 데이터 B 입력의 생성기이고 그 README가 이것을 '서비스 표 찾기의 기준 구현'이라 부른다(같은 서버의 `docs/LABEL_TABLES.md`는 같은 규칙의 표 문서). 이전 판은 표를 학습 샘플에서 역으로 읽은 초안이었고 이 원본을 모르고 만든 것이었다. 지금은 원본을 옮겼다: 어휘와 고정 문장은 `schema/data/response_tables.json`(계약 `ResponseTables`, `table_status: DATASET_RULES_UNCONFIRMED`, `/readyz`의 `response_tables.status`), 조합 로직(그룹핑, 조사, 시간·년 표기, 술 단위, previous/current)은 `survey_service/response_tables.py`(`b_fields`), 조립은 `extraction.py`다. 확정 여부: 표 승인 기록을 찾지 못했다(IO_SPEC 7장 2번은 미정). 서비스가 따를 기준은 학습 데이터가 이 규칙으로 만들어졌다는 점이다. 서버 파일의 내용은 리포에 복사하지 않았고 출처만 적었다(시험용 대표 쌍 `tests/data/response_tables_golden.json`은 규칙 함수의 출력이지 학습 데이터 원문이 아니다).

| B 입력 | 만드는 법 (원본 규칙) |
|---|---|
| `topic` | 지금 다루는 문항: 대상 칸(`targets[0]`)의 문항, 없으면 첫 값(`values[0]`)의 문항, 없으면 입력 `question_id`. 문항별 문구는 표(질환 문항은 질환 이름 틀) |
| `known` | CONFIRM이면 빈 문자열. 그 밖에는 상태가 ANSWERED·CORRECTION이고 값이 있는 모든 값(`save` 여부와 무관)을 칸별 문장으로 이어 쓴다. 같은 문항의 횟수+기준, 술 종류+양+단위는 한 덩어리(`일주일에 7번`, `소주 2병쯤`). 지금 문항이 아닌 문항 값 앞에는 짧은 이름(`일반담배: ...`). 근사값은 숫자 뒤 `쯤`(술은 단위 뒤) |
| `ask` | REASK·CONFIRM: 사유와 대상 칸별 문장을 `, `로 잇고 같은 문장은 하나로. 문장은 칸과 맥락(술 종류, 이미 말한 단위·양, Q7 횟수·기준)으로 정해진다. 원본에 없는 사유는 원본의 일반 분기(`<칸 이름:을/를> 다시`) |
| `previous`, `current` | CONFIRM: current는 CONFLICT 값(술은 단위 포함, Q7 횟수만이면 저장된 기준 기간), previous는 대상 칸의 저장 값. 사유별 예외(`평소 ...`, `지난 1년 술 안 마심`) |
| `hold` | HOLD·SKIP: 대상 문항의 짧은 이름. 흡연(Q4_1·Q5_1)과 질환(Q1.*) 문항에서 한 칸만 비우면 `일반담배 중 피운 기간` |
| `note` | A 출력의 note가 있으면 그대로 |
| `next` | **문항 은행의 질문 문장**(데이터의 `next.text`는 같은 문항도 여러 문장으로 바꿔 쓴 것이라 데이터를 따르지 않는다). END, SAME(REASK·CONFIRM·REDIRECT), NEXT. 아래 57행 처리만 다르다 |

**확인한 것** [`scripts/table_agreement.py`: 학습 서버 `~/WS/dataset/data/final/lora_a_*.jsonl`, `lora_b_*.jsonl`의 train·val·test를 표준출력으로 흘려 이 PC에서 계산했고 저장하지 않았다. A 행과 B 행은 `scenario_id`·발화·action·reason으로 짝지었고 모든 B 행이 A 행과 정확히 하나로 짝지어졌다]: START를 뺀 B 입력 5,077/5,077행(train 3,982, val 549, test 546)에서 서비스가 만든 `next`를 뺀 필드가 글자 단위로 같고, START 110/110행의 `topic`·`next.type`·`next.question_id`가 같고, `next.type`·`next.question_id`가 5,077/5,077 같다. 원본 코드와의 조합 비교(서버 데이터 없이): `ask_text` 20,332 조합(사유 x 대상 칸 x 맥락)과 다중 대상 136개, `value_text` 칸 x 값 x precision 930개와 표 밖 값 204개, 무작위 `b_input` 40,000개가 전부 같았다(원본이 예외를 던지는 16개는 서비스도 같은 예외 종류). **한계**: 일치는 '학습 데이터와 같은 문장을 낸다'는 뜻이고 표가 옳다는 뜻이 아니다(데이터셋 담당의 승인 필요). `next.text`는 학습 데이터와 다르다(문항 은행 문장).

**폴백**: 원본의 일반 분기(`else`)가 만드는 문장은 원본이 만드는 문장이라 폴백이 아니다(`/readyz`에 따로 세지 않고 `stats.ask_generic`). 학습 데이터 전수에서 이런 (사유, 칸) 조합은 `UNPARSED_VALUE` x `Q10.days` 2건뿐이다(문장 `날 수를 다시`). `ask_fallback`, `phrase_fallback`은 **원본 규칙이 예외를 던질 입력**(표에 없는 문항, 표에 없는 Q3 값)에서만 센다: 이때 서비스는 문장을 지어내지 않고 topic 또는 칸 이름 문장으로 대신한다. 학습 데이터 전수에서 두 카운터는 0이다.

**57행 문제**(같은 말에서 일부 칸이 이미 채워진 문항으로 NEXT; 데이터에서 Q4->Q4_1 25+3건, Q5->Q5_1 28+1건 모두 `Q4_1.status`/`Q5_1.status`가 이미 저장됨): 학습 데이터의 `next.text`는 이미 저장된 칸을 다시 묻는 문장(`요즘 일반담배를 피우고 계신가요?`)이라 모델이 받은 내용을 다시 묻는다. 서비스는 **남은 칸만 묻도록** 조립한다: 표의 문항 칸 설명(`slots`, `only_if`)에서 저장된 칸을 빼고 `일반담배는 피운 기간, 하루 개비 수, 끊은 지 얼마나 됐는지 말씀해 주세요.`처럼 만든다(현재 흡연자면 `끊은 지`는 묻지 않는다). 근거: (a) 같은 말에서 저장했다고 `known`에 말한 값을 곧바로 다시 묻는 것은 사용자에게 틀린 응대고 저장 기록(이미 있음)과 어긋난다, (b) 계약은 이 NEXT를 허용한다(V2_DRAFT 3장), (c) 서비스는 값을 지어내지 않고 표에서 문장을 만든다. **위험**: 이 문장은 학습 분포에 없다(학습 데이터는 이미 답한 칸을 다시 묻는다). B 모델이 이 `next.text`를 어떻게 다루는지는 확인하지 못했다. 남은 칸이 없으면(모든 칸이 이미 저장) 문항 은행 문장 그대로 보낸다(이때 A가 이미 다 채워진 문항을 다음으로 지목한 것이라 건너뛰기는 A의 몫이다). 칸 설명이 표에 있는 문항은 Q4_1·Q5_1뿐이다(데이터에서 이 현상이 나온 곳).

## 19. Trace 키, 오류, 위험 신호

**19.1 TurnTrace 키(최종)** [확인: `schema/schemas/TurnTrace.json`, ops-feedback이 읽는다]. 모든 키는 필수이고 값이 없으면 null이다(`turn_index`만 선택).

| 키 | 값 | v1 키 |
|---|---|---|
| `session_id`, `turn_id`, `contract_version` | 그대로 | 같음 |
| `frontend_message`, `intervention_decision`, `orchestrator_input`, `orchestrator_output`, `frontend_input`, `staff_console_input`, `dispatch_input`, `dispatch_output`, `session_before`, `session_after`, `log_entry`, `persistence_load_request`, `persistence_load_result`, `persistence_commit_request`, `persistence_commit_result`, `turn_index` | 형식은 새 `SessionStore`, `LogEntry`, `TurnWriteSet`로 바뀜 | 같음 |
| `extract_input` | LoRA-A 입력(`ActionExtractionInput` 사본). 비발화·처리 안 함 턴은 null | `parser_input` |
| `extract_output` | LoRA-A 출력(`ActionExtraction` 사본). 마지막 호출의 것, 503으로 못 받았으면 null | `parser_output` |
| `extract_attempts` | 서비스가 부른 횟수 1~2 (D75 재시도 포함) | 신규 |
| `extract_status` | `OK`, `OUTPUT_INVALID`(503 두 번), `VIOLATION`(검증 위반 두 번), `ERROR`(그 밖의 호출·입력 오류) | 신규 |
| `extract_protections` | `X-Openit-Protections`를 푼 `{규칙 코드: 건수}` 또는 null | 신규 |
| `validation_ok` | `validate_extraction`+서비스 검사 통과 여부 | 신규(`normalizer_*`, `judge_*` 대체) |
| `validation_violations` | 위반 `[{code, path, message}]`(마지막 시도, 값 없음). 통과하면 `[]`, 검증하지 않았으면 null | 신규 |
| `response_input` | LoRA-B 입력(`ResponseRequest` 사본). 고정 문장·응답 없음 턴은 null | `generator_input` |
| `response_output` | 응답 문장 또는 null | `generator_output` |
| `response_outcome` | `ok`, `fallback`(서버가 대체 문장), `fixed`(문항 세트의 고정 문장, 모델 없음) | 신규 |
| `response_protections` | `extract_protections`와 같은 모양 | 신규 |

없어진 키: `parser_input`, `parser_output`, `normalizer_input`, `normalizer_output`, `judge_input`, `judge_output`, `record_writer_input`, `record_writer_output`, `planner_input`, `planner_output`, `generator_input`, `generator_output`. 저장 결정은 `persistence_commit_request.writes`(`answer_updates`, `progress_record_update`)에 있다. `LogEntry`(대화 로그, `session_*`와 `log_entry`에 들어감)의 모델 관련 칸은 `extract_output`, `validation_violations` 둘로 줄었다(`directive`, `flow`, `progress_record_update`, `parser_output` 등 삭제). `FailedStep.step`은 `LoRA-A`, `VALIDATE`, `COMMIT`, `DELIVER_STAFF`, `LoRA-B`, `DELIVER_FRONT`(`NORMALIZE`, `JUDGE`, `RECORD_WRITE`, `PLAN` 삭제).

**19.2 공개 API는 바뀌지 않았다**(PUBLIC_API.md). `FrontendInput`, `SessionState`, 오류 코드, 직원 알림 API의 형식과 시험(`test_public_api*.py`, `test_staff_alerts.py`)은 그대로다. 응답 문장은 모델 출력이거나 고정 문장이다.

**19.3 추론 호출**: `remote.py`는 `boundary_contracts.clients.HttpModelRuntime`(extract JSON, respond text/plain)을 쓴다. 시험은 계약의 `RuntimeReferenceServer`(`conformance.servers`, 계약 저장소의 conformance/servers.py 복사본)에 사고 주입(`tests/runtime_support.py`)을 붙여 한다. `inference-server-spec.md`의 v1 규약은 HTTP.md I9 v2로 대체되었다.

**19.4 위험 신호·직원 알림: 미정, 헤드 결정 필요(CR-012)**. v1이 직원 알림을 만든 곳과 읽은 것(현재 코드가 아니라 turn-engine 0.3.0의 폐기 표시된 `planner.py`와 v1 서비스 코드에서 확인한 것): (1) 파서의 `risk_signal` 의도와 문항 세트의 `session_settings.risk_phrases`(판정기가 `utterance_judgment.risk_signal`로) -> `RISK/RISK_SIGNAL` 알림과 세션 정지(`PAUSED_RISK`), (2) 문항 세트 슬롯의 `risk_rule`(IMMEDIATE)과 저장 값 -> `RISK/RISK_RULE`, (3) 파서의 `STAFF_REQUEST` 요청 -> `USER_CALLED_STAFF`, (4) Plan의 물음 상한·못 알아들음 상한 -> `CALL_STAFF`. **v2의 A 출력(`ActionExtraction`)에는 이 신호가 없다**: action은 NEXT·CORRECT·REASK·CONFIRM·HOLD·SKIP·REDIRECT, REDIRECT 사유는 OUT_OF_SCHEMA·OFF_TOPIC·ASK_PURPOSE뿐이고 `note`는 OUT_OF_SCHEMA의 요약이다. 따라서 서비스는 알림을 **만들지 않는다**(지어내지 않는다). 알림 저장·전달·SSE·푸시·해결 경로와 정지 세션의 직원 재개·종료 경로는 코드를 그대로 두었다. `Orchestrator(risk_source=...)`(Protocol `RiskSource`, 설정 `SURVEY_RISK_SOURCE`)에 입력원을 꽂으면 켜진다. 꽂지 않았을 때는 `/readyz`의 `staff_alert_source: {active: false, reason: NO_INPUT_SOURCE_IN_V2_EXTRACTION}`로 드러낸다. 선택지(결정은 헤드): 서버에서 위험 신호를 내는 모델/규칙 층을 따로 둘지, 문항 세트의 `risk_rule`을 저장 값에 서비스가 평가하는 것을 허용할지(이는 판단 코드를 서비스에 두지 않는다는 D74(1)와 부딪힌다), A에 필드를 더하도록 데이터셋 담당에 요청할지, 아니면 직원 알림은 키오스크의 도움 요청 버튼 같은 다른 경로로 받을지. 시험·스모크용 대역 `ScriptedRisk`만 있다.
