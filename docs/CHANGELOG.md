# 변경 이력

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
