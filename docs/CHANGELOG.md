# 변경 이력

## 직원 세션 조회, 위험 규칙 엔진, 위반 코드 관측, 타임아웃 분류 정정, 결과 전달 대상 없음 [미출시, 푸시·태그 전]

비어 있는 요구사항은 임의로 채우지 않고 표준적인 방법을 골랐다. 고른 이유는 항목마다 한 줄.

- **직원 세션 조회** `GET /v1/staff/sessions/{session_id}`(응답 `StaffSessionView`, 스키마 `schema/schemas/StaffSessionView.json`, `COMPONENTS.yaml`의 Staff Console에 `view`). 기존 직원 토큰·역할 검사를 그대로 쓰고 없으면 404. 출처는 세션 문서와 AlertStore. PUBLIC_API.md는 소유자가 달라 CR-013에 추가할 문구를 썼다.
  - 이유(접근 기록): 환자 데이터를 읽는 호출은 누가 언제 어느 세션을 읽었는지 남기는 것이 의료 정보 열람의 표준 통제라서, 알림 DB의 새 테이블 `staff_access_log`(시각, 역할 이름, session_id, 결과)에 호출마다 남긴다. 토큰 값·발화는 넣지 않는다. 남기지 못하면 데이터를 주지 않는다(기록 없는 열람 금지, fail-closed).
  - 이유(시각 테이블): 세션 문서에 시각과 turn_id가 없어(스키마를 바꾸면 정수 버전과 저장소 이전에 걸린다) `session_timeline` 테이블에 턴마다 남긴다. 이전 세션은 null.
  - 이유(`rule_id` 열): `alerts` 테이블에 열을 더했다(기존 파일은 시작할 때 `ALTER TABLE`로 이행). 알림 목록·SSE 모양은 바꾸지 않았다.
- **문진 스키마 정수 버전(`CONTRACT_VERSION`)은 1 그대로**(올리지 않음). 판단: 새 계약 파일 하나를 더하는 것은 덧붙임이라 기존 소비자(model-runtime은 계약 이름으로 읽는다)가 깨지지 않고, `StaffSessionView`는 이 서비스와 프런트만 쓰는 읽기 응답이다. 정수 버전은 같은 이름의 기존 계약을 비호환으로 바꿀 때 올리는 값(design.md의 정수 버전 설명)이라 해당하지 않는다. 단 스키마 패키지의 계약 개수가 13에서 14가 되어 `tests/test_schema_package.py`의 기대값을 바꿨다. 헤드가 다르게 보면(예: 패키지 MINOR 표시) 알려 주세요.
- **위험 감지 규칙 엔진** `SURVEY_RISK_RULES_FILE`(`survey_service/risk_rules.py`). 환자 발화가 규칙에 걸리면 기존 `RiskSource` 경로(RISK 알림, `PAUSED_RISK` 정지, 고정 문장)를 탄다. 기본 규칙 파일 없음(없으면 비활성), 임상 키워드를 담지 않는다. 시험은 "시험위험문구"만 쓴다. 데모 `examples/risk-rules.demo.json`은 '시험용, 임상 규칙 아님'.
  - 이유(evidence): 알림 이유 스키마(`RISK_SIGNAL.evidence`)는 비어 있지 않은 문자열을 요구하고 `RISK_RULE`은 슬롯 목표를 요구해서, 스키마를 바꾸지 않고 `rule:<규칙 id>`를 evidence에 담는다. 발화 원문은 알림·푸시·접근 기록·정지 기록·로그에 가지 않는다.
  - 이유(맞춤): 외부 규칙 파일의 표준적인 두 방식(부분 문자열, 정규식)만 두고, 발화와 문구를 유니코드 NFC로 맞춘 뒤 문구는 대소문자 무시(한글 표기 차이로 놓치지 않으려고). 규칙은 파일 순서, 처음 걸린 하나만 쓴다.
  - 파일 sha256·version·규칙 개수를 시작 로그와 `/readyz`의 `staff_alert_source`에 낸다. 파일 오류는 시작 거부(종료 코드 3). `SURVEY_RISK_SOURCE`와 함께 쓰면 설정 오류(입력원은 하나).
  - 한계: 규칙은 A 출력이 검증을 통과한 턴의 `plan_writes`에서 평가된다. A 호출 실패·검증 실패 턴의 발화는 평가되지 않는다. 안전 기능으로는 부족할 수 있어 헤드 판단이 필요하다(검증 앞으로 옮기면 턴 흐름이 바뀐다).
- **의미 검증 실패 사유 관측**. 위반 코드와 경로를 로그(`의미 검증 위반 ...`, 최종 실패 줄의 `violations=`)에, 코드별 빈도를 `/readyz`의 `validation_violations`에 남긴다. 발화·값·메시지는 제외한다. HTTP 응답 본문은 바꾸지 않았다. 이유: 원인을 구분하려면 코드가 있어야 하고, turn-engine이 이미 값을 싣지 않는 `code`·`path`를 주므로 그것만 쓴다.
- **조사 (4) 추론 타임아웃 분류**. 5.65초에 timeout으로 분류된 원인: 추론 호출의 타임아웃은 `SURVEY_INFERENCE_TIMEOUT_SECONDS`(기본 **4초**)이고, `SURVEY_HTTP_TIMEOUT_SECONDS`(10초)는 릴리스 읽기·Trace 전송에만 쓰인다(`clients.py`). 분류 함수 `_timed_out`은 `UNAVAILABLE`의 걸린 시간이 타임아웃의 90%(3.6초) 이상이면 오류 종류와 관계없이 timeout으로 셌다. 그래서 4초 설정에서 5.65초 걸려 실패한 호출은 읽기 타임아웃뿐 아니라 느린 502·503 응답이나 릴리스 재적재 대기도 timeout으로 분류됐다(이 경우 재시도도 건너뛴다). 5.65초가 4초를 넘은 것은 urllib의 타임아웃이 소켓 동작마다 적용되는 값이라 합계가 넘을 수 있어서다. 어느 경우였는지는 당시 로그에 reason이 없어 **확인하지 못했다**. 고침: 서버 응답(`status`가 있다)이나 이 서비스가 직접 낸 사유(`RELOADING`)는 timeout이 아니다. `reason=TimeoutError`는 항상 timeout, 연결 단계 타임아웃(`URLError`로 감싸짐)만 걸린 시간으로 가른다. 실패마다 `추론 호출 실패 op= code= reason= status= elapsed= read_timeout= classified=` 로그를 남긴다. 시험 `test_timeout_classification`.
- **조사 (4) 결과 전달**. `결과 전달 실패 turn=end error=RuntimeError`의 원인: Result Dispatcher가 꽂히지 않으면 기본 객체 `UnattachedDispatcher.dispatch`가 `RuntimeError`를 던지게 되어 있었고, graph가 그 예외를 일반 전달 실패로 경고 로그에 남겼다. `COMPONENTS.yaml`이 결과 전달을 "붙을 예정"으로 두었으므로 대상이 없는 것은 정상 구성이다. 고침: `attached=False`인 전달자는 부르지 않고 INFO `결과 전달 대상 없음 status=NO_TARGET`, 카운터 `dispatch_no_target`, `/readyz`의 `result_dispatcher`로 명시한다. 세션의 전달 기록은 계약대로 `succeeded=false`(전달되지 않았다)이고 `dispatch_called`는 그대로 true다(턴 출력 모양 유지). 꽂은 전달자가 실패하면 기존대로 경고다.
- 시험: `tests/test_staff_view_and_rules.py`(조회·스키마·404·401/403·접근 기록·기록 실패 거부·규칙 파싱/매칭/readyz/시작 거부·알림에 원문 없음·위반 코드·타임아웃 분류·대상 없음). 전체 pytest 448 통과(이전 415), `scripts/smoke.sh`는 STATUS 참조.

## 의존 태그 올림: model-runtime v0.7.0 -> v0.7.1 [미출시, 푸시·태그 전]

- model-runtime 의존을 v0.7.1로(requirements, build_wheels 기본 태그, inference.py 주석, boundary·runbook·STATUS의 현재 기준 표기). v0.7.1은 가짜 백엔드가 값을 내도록 확장(흐름 파일 선택 항목 slots/skips)이고 HTTP·계약 모양은 그대로다. 서비스 코드 변경 없음.
- 시험: 가짜 응답 형태(NEXT + values 1건)가 검증 -> 저장 -> B 입력 조립을 지나 세션 상태에 저장되는 것은 기존 `tests/test_orchestrator.py::test_full_conversation`이 확인한다.

## 의존 태그 맞춤: turn-engine v0.4.0, model-runtime v0.7.0 [미출시, 푸시·태그 전]

- 의존을 turn-engine v0.4.0, model-runtime v0.7.0으로(requirements, build_wheels 기본 태그, 이미지 라벨, inference.py 주석, 문서 표기). turn-engine v0.4.0은 v1 폐기 함수를 제거했고 `validate_extraction`·`QuestionFlowTable`·`SkipRule`·`DATASET_V2_FLOW`·`ContractViolation`·`ENGINE_VERSION`은 그대로이며 boundary v2.0.2를 고정한다. model-runtime v0.7.0은 비밀 파일화와 prompt-3 제거(serving_config `prompt_template_versions=['prompt-4']`)이고 `RUNTIME_VERSION`이 0.7이다.
- 릴리스 기대값은 `model_runtime_version` 0.7.x, `turn_engine_version` 0.4.x. 0.6.x 릴리스 매니페스트는 MAJOR.MINOR가 달라 `VERSION_MISMATCH`로 거부되므로 재생성해야 한다. prompt-3 릴리스는 이제 적재되지 않는다(`UnsupportedServingConfig`, 이전 릴리스 유지). 이전의 "적재하고 경고만" 동작은 v0.6.x 시점의 기록이다.
- 시험: 버전 기대값을 0.7./0.4.로, prompt-3 시험을 거부 시험으로 바꿨다. 서비스 코드 변경 없음.
- 문서 정정: design.md 17절 도식과 CR-001의 v1 함수 이름에 '역사 기록' 표시를 붙였다.

## 되묻기 상한 메커니즘(G12)과 직원 알림 입력원 포트 문서(G01) [미출시, 푸시·태그 전]

- `SURVEY_MAX_CONSECUTIVE_REASK`(기본 0=끔): 연속 REASK·CONFIRM 횟수를 대화 로그에서 세고, 상한을 넘으면 기존 직원 호출 경로로 넘긴다. 값은 정하지 않았다. `/readyz`에 `reask_limit`. 시험 `tests/test_reask_limit.py`.
- 직원 알림 입력원 포트(`RiskSource`)와 `SURVEY_RISK_SOURCE` 선택을 문서로 고정하고 가짜 입력원 시험을 추가했다(`tests/test_alert_source_port.py`). 입력원은 정하지 않았다.

## 비밀 파일화(D84), boundary-contracts v2.0.2 [미출시, 푸시·태그 전]

- 모든 토큰·비밀을 `boundary_contracts.read_secret`으로 읽는다(`NAME_FILE` 우선, 없으면 `NAME`). `*_TOKEN_ENV` 간접 지정은 의미를 보존한다. `/readyz`에 `secrets`(출처만 file|env|unset) 추가. 값이 로그·`/readyz`·오류·저장소 파일에 없는 시험 추가(`tests/test_secrets.py`).
- 의존을 boundary-contracts v2.0.2로(requirements, build_wheels, 문서). turn-engine v0.3.1 메타데이터는 아직 v2.0.1을 가리키며 이미지는 wheel을 `--no-deps`로 설치하므로 영향이 없다. 열차 태그 뒤에 맞춘다.

## 의존 태그 맞춤: turn-engine v0.3.1 [미출시, 푸시·태그 전]

- 의존을 turn-engine v0.3.1로(requirements, build_wheels 기본 태그, 이미지 라벨, 문서 표기). v0.3.0 태그는 boundary-contracts v2.0.0을 고정해 v2.0.1과 충돌했고, v0.3.1이 boundary v2.0.1을 고정한다. 코드 변경 없음.

## 의존 태그 맞춤: boundary-contracts v2.0.1, model-runtime v0.6.5 [미출시, 푸시·태그 전]

- 의존을 boundary-contracts v2.0.1, model-runtime v0.6.5로(requirements, build_wheels 기본 태그, 이미지 라벨, 문서 표기). v2.0.1은 ActionExtraction properties 순서만 사전순으로 바꾼 PATCH이고 TraceEnvelope 예시를 v2 trace로 고쳤다. model-runtime v0.6.5는 제약 디코딩 relax-3(if/then를 anyOf로)이며 HTTP 모양은 그대로다. 코드 변경 없음. requirements.lock에는 두 항목이 없어 갱신하지 않았다.

## model-runtime v0.6.4 맞춤 [미출시, 푸시·태그 전]

- 의존을 model-runtime v0.6.4로(requirements, build_wheels 기본 태그, 이미지 라벨, 문서 표기). v0.6.4는 제약 디코딩 정책 relax-2(pattern/minItems 전달)와 OUTPUT_INVALID 응답 `details.violations` 추가이며 HTTP 모양은 그대로다. 코드 변경 없음. OUTPUT_INVALID 처리는 기존대로 재시도 1회 후 turn_failure_signal. relax-1이나 `details.violations`에 의존하는 코드·문서는 없었다.

## model-runtime v0.6.3 맞춤 [미출시, 푸시·태그 전]

- 의존을 model-runtime v0.6.3으로(requirements, build_wheels 기본 태그, 이미지 라벨, 문서 표기). 코드 변경 없음.

## model-runtime v0.6.2 맞춤 (브랜치 follow-mr-0.6.2) [미출시, 푸시·태그 전]

- 의존을 model-runtime v0.6.2로(requirements, build_wheels 기본 태그, 이미지 라벨 `io.openit.model-runtime=v0.6.2`; D72·D80). 라이브러리 API 변화 없음. 코드 변경은 주석·문서의 버전 표기뿐.

## model-runtime v0.6.1 맞춤 (브랜치 follow-model-runtime-0.6.1) [미출시, 푸시·태그 전]

- 의존을 model-runtime v0.6.1로(requirements, build_wheels 기본 태그, 이미지 라벨 `io.openit.model-runtime=v0.6.1`). 릴리스 기대값은 `prompt-4`·`protect-2`·`model_runtime_version` 0.6.x. 시험 fixture는 `model_runtime.fake_serving_config()`의 기본(prompt-4)을 따른다.
- 릴리스 적재: `prompt-3`(model-runtime이 폐기 예정으로 표시)은 거부하지 않고 적재하되 경고를 `release_loader.warnings`에 남긴다. 목록은 설치된 model-runtime의 `DEPRECATED_TEMPLATE_VERSIONS`다.
- `scripts/check_serialization_against_val.py`: 서비스가 만든 A/B 입력을 학습 서버 val 행과 대조한다(design 17절 끝).

## 0.2.0 제안 (브랜치 migrate-contracts-2.0.0, 계약 2.0.0, D74·D75·D76) [미출시, 푸시·태그 전]

**파괴적 변경**: v1 레코드는 읽지 않는다. TurnTrace 키가 바뀌었다(ops-feedback 영향, design 19.1).

- 한 턴의 구조: 파서·정규화·판정·기록·Plan·생성 노드를 `extract`(LoRA-A 한 번) + `validate_extraction`(turn-engine 0.3.0) + `plan_writes`로 대체. 세션 시작은 B만(`action=START`). 503 OUTPUT_INVALID·검증 위반은 같은 요청 1회 재시도 후 `turn_failure_signal`(D75).
- 추론 호출은 `boundary_contracts.clients.HttpModelRuntime`(extract JSON, respond text/plain, model-runtime 0.6.0). 시험은 계약의 `RuntimeReferenceServer`로.
- 문진 스키마(정수 버전 1 유지): 문항 순서·건너뛰기 표 `QuestionFlow`(D76), 표 찾기 표 초안 `ResponseTables`(미확정), 데이터 파일 `schema/data/`, `load_data()`. `SessionStore`(저장은 bool·수, `progress_record` 재구성), `LogEntry`, `TurnTrace`, `DispatchInput` 변경. v1 모델 입출력 계약 12개 삭제. **같은 정수 버전 안의 비호환 변경이다**(정수 버전을 올릴지는 헤드 결정, design 17절).
- Trace 키: `parser_*`, `normalizer_*`, `judge_*`, `record_writer_*`, `planner_*`, `generator_*` 삭제 -> `extract_*`, `validation_*`, `response_*` 신설.
- 표 찾기: `topic`, `known`, `ask`, `previous`, `current`, `hold`, 남은 칸 문장(57행 문제)을 학습 데이터에서 역으로 읽은 초안 표로 만든다(design 18절, `scripts/table_agreement.py`).
- 직원 알림: 입력원이 없어(v2 A 출력에 위험 신호 없음) 비활성, `/readyz`에 드러냄. 경로는 유지(CR-012).
- 의존: boundary-contracts v2.0.0, turn-engine v0.3.0, model-runtime v0.6.0(이미지 라벨 포함). 릴리스 매니페스트는 역할별 `system_prompt`를 가진 것만 적재된다(prompt-3, protect-2, model_runtime_version 0.6.x).
- 삭제: 골든 대조(`tests/test_golden.py` 등), bindings 대조, 서비스 안의 v1 추론 서버 대역(`tests/ref_inference_server.py`), `turn_engine` 정규화·판정·기록·Plan 호출.
- 공개 API(PUBLIC_API.md)는 바꾸지 않았다.

## 0.1.0 (미출시, 2026-10-15)

- 문진 스키마 패키지 `openit-survey-schema` 1.0.0 (`schema/`): 정수 버전별 디렉터리, `load_schema(name, version=1)`, `schema_dir(version=1)`. `CONTRACT_VERSION`(1)과 스키마 파일은 바꾸지 않았다.
- survey-service 0.1.0: Orchestrator(처리 표 9-1, 쓰기 확정, 확정 뒤의 일, 실패 신호, 턴 멱등), Session Store(SQLite·메모리, 반영 효과 9-2), 릴리스 로더(승인·해시·계약 버전 검사, 거부 시 이전 유지, 새 세션부터 적용), 추론 서버(fake 백엔드), Trace outbox와 내보내기(최소 1회, UNAVAILABLE만 재시도), `/healthz` `/readyz`, 개발용 CLI.
- 공개 API(PUBLIC_API.md, D55): 세션 시작·발화(turn_id 멱등)·종료, 인증·검증·상태 코드, 유휴 만료, 스모크에 공개 API 한 턴. 모델 런타임을 HTTP 경계 뒤에 두는 추론 서버(`inference_server`, `SURVEY_INFERENCE_URL`). 세션 저장소에 `updated_at`(이전 파일은 자동 이행). CR-009.
- 직원 개입 경로(D58): `POST /v1/sessions/{id}/intervention`, 역할 staff 토큰 분리(403/401), 스모크에 멈춘 세션을 풀어 이어가는 한 바퀴.
- 의존: boundary-contracts v1.3.1, turn-engine v0.2.0, model-runtime v0.4.0(역할별 serving_config, vLLM 백엔드는 아직 연결하지 않음).
- I4·I6를 HTTP 클라이언트(`HttpReleaseReader`, `HttpTraceIngest`)로 연결(`SURVEY_RELEASE_READER=http` 등, 토큰은 환경 변수 이름으로 지정), 번들은 `dest/<릴리스 ID>`를 쓴다. 참조 서버 통합 시험(`tests/test_http_integration.py`). 이미지 라벨 6개(`io.openit.*`).
- 컨테이너: Dockerfile, `scripts/build_wheels.sh`, `scripts/smoke.sh`.
- 골든 대조: 226 케이스, 비교 1220쌍 중 1215 일치, 5건은 예시 오류(CR-008).
- 헤드 결정 반영: bindings.yaml 대조(`tests/test_bindings.py`), 라이브러리 MAJOR.MINOR 불일치 거부, 외부 ID `x-` 변환, 역할별 serving_config 처리 분리.
- 변경 요청: CR-001(스키마 변경 후보, 적용 안 함) ~ CR-008.

## 후속 (D62)

- 오케스트레이터를 LangGraph `StateGraph`로 전환(`survey_service/graph.py`). 처리 표 9-1의 열은 조건부 간선, 행은 열 노드의 표. 동작 불변(기존 시험·골든·스모크 통과). 상태는 직렬화 가능, 체크포인터 없음. langgraph 1.2.15 잠금 고정, 이미지 라벨 `io.openit.langgraph`, 외부 추적 끔.
- 추론 서버 규약(`docs/inference-server-spec.md`, 기준은 boundary-contracts HTTP.md I9), 서비스 안의 추론 서버를 시험 대역으로 이동, 실패·지연 처리, 보호 건수(규칙별) 노출, `scripts/smoke_compose.sh`.

## 후속 (D64, D66)

- 직원 알림: 영속 저장(SQLite), 목록, SSE(이벤트 순번으로 이어 받기), 웹 푸시(pywebpush, VAPID 환경 변수, 없으면 SSE만), 구독 등록·해지(멱등), 개입 시 해결 표시, 전달 판정(DELIVERED/DELIVERY_FAILED). `StaffConsole` 접점의 기본 구현으로 정리(교체 가능).
- 공개 API 보완(boundary-contracts v1.3.2): 세션 상태(`resume_text`), 푸시 설정, 오류 코드 표, Retry-After, turn_id 봉투, 폴링 빈도 상한. 응답을 boundary-contracts의 StaffAlert/StaffAlertPage/SessionState/PushConfig/BoundaryError 스키마로 검증하는 시험.

## 수정 (fix/loader-retry)

- 릴리스 로더: 일시적 적재 실패(추론 서버가 아직 뜨는 중 등)를 영구 거부로 확정하던 결함을 고쳤다. 백오프로 같은 포인터를 계속 다시 시도하고(`/readyz` `release_loading`, `release_loader.retrying`), 결정적 거부만 영구 거부로 둔다. 시작할 때 첫 적재를 기다리는 시간에 상한을 두어 /healthz가 적재 동안에도 산다.
