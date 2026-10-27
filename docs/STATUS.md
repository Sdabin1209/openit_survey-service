# 상태 (2026-10-15, 첫 구현 구간)

## 한 일

1. **설계서와 스키마 패키지**: `docs/design.md`. `schema/`를 설치 가능한 패키지(`openit-survey-schema` 1.0.0, import `survey_schema`)로: `pyproject.toml`, `setup.py`(빌드 때 원본을 `survey_schema/v1/`로 복사), `load_schema(name, version=1)`, `schema_dir(version=1)`, 여러 정수 버전 지원. `CONTRACT_VERSION`과 스키마 파일은 그대로(`git diff`로 확인). 태그는 헤드가 붙인다.
2. **Orchestrator·Session Store**: 처리 표, 한 턴 흐름, 쓰기 확정, 확정 뒤의 일, 실패 신호, 턴 멱등. 반영 효과(9-2)는 순수 함수. SQLite·메모리 저장소.
3. **릴리스 로더·추론 서버·Trace 내보내기**: 포인터 폴링, 승인·해시·계약 버전 검사, 거부 시 이전 유지. outbox와 전송 스레드. `/healthz` `/readyz`. 개발용 CLI.
4. **골든 대조·컨테이너**: 226 케이스 대조(CR-008), Dockerfile, `scripts/build_wheels.sh`, `scripts/smoke.sh`.
5. 문진 스키마 변경 후보 3건은 **적용하지 않고** CR-001에 영향 영역과 함께 정리했다.

## 시험 결과 (실제로 돌린 것)

- `.venv/bin/python -m pytest` : 148 통과.
- 골든: 226 케이스, 처리한 턴 234, 비교 1220쌍 중 1215 일치, 5 불일치 모두 (c) 예시 오류. 골든 표본 1704개 모두 스키마 통과.
- `bash scripts/smoke.sh` : SMOKE OK (이미지 빌드, `/healthz`, `/readyz` 준비됨, 비root, 라벨, 이미지 안의 한 턴, SIGTERM 정상 종료, 필수 설정 없으면 종료 코드 2, 공개 API 경로는 404).

## 하지 못한 것 / 한계

- **실제 이웃과의 통합**: release-gate(I4)·ops-feedback(I6)의 배포된 SDK가 없어 `boundary_contracts.sdk` Protocol을 따르는 대역(`tests/fakes.py`)으로만 시험했다. 실제 SDK가 나오면 팩토리 설정으로 꽂고 같은 시험을 실제 구현으로 돌려야 한다.
- **실제 모델**: GPU·실제 백엔드가 없어 FakeModelSession만 썼다. 추론 서버를 별도 GPU 컨테이너로 나누는 일, 모델 출력 품질은 확인하지 못했다.
- **골든의 한계**: 골든에 문항 세트와 모델·엔진 입력이 없어 실제 turn-engine을 골든 입력으로 돌린 비교는 하지 못했다. Orchestrator는 컴포넌트 출력을 재생해 비교했다(CR-002는 닫음). 입력 조립은 bindings.yaml과 대조했다.
- CI, digest 게시, deploy 제출은 없다(D24).
- 부하·동시성(여러 프로세스), 세션 문서 암호화·보존 기간 없음.

## 막힌 것 / 헤드에게 필요한 것

- 스키마 패키지 Git 태그(헤드가 붙임).
- CR-001: 스키마 v2 후보 3건(헤드가 모아 한 번에 올릴 때까지 보류).
- O06: 공개 API, 턴 ID 발급, 프론트 전달 실패의 뜻.

## 다음에 할 일

1. 실제 I4·I6 SDK로 통합 시험(대역과 같은 적합성 시험).
2. 실제 백엔드가 생기면 `InferenceServer`에 추가하고, 분리 배포가 필요하면 `ModelSession` 원격 구현.
3. 직원 콘솔·결과 전달·프론트 전달 어댑터(O06 후).
4. 골든 보강본(CR-002)이 오면 실제 turn-engine 대조 추가.

## 후속 구간 (헤드 답변 반영)

- CR-004 해결: `docs/rules/sources/bindings.yaml`과 필드 단위 대조(`tests/test_bindings.py`). 차이 1건(판정기 `affected_followups`)을 고쳤고, 표가 정하지 않은 선택 정책은 design.md 3.1에 근거와 함께 적었다.
- CR-005 결정 반영: 릴리스의 turn-engine·model-runtime MAJOR.MINOR 불일치는 적재 거부(`VERSION_MISMATCH`, `/readyz`에 사유), PATCH는 경고.
- CR-007 결정 반영: 외부 세션·턴 ID는 `Id` 형식이 아니면 `x-`+SHA-256 앞 32자로 봉투에 넣는다(Trace를 잃지 않는다).
- CR-006 반영: model-runtime v0.3.0으로 올림(requirements, wheel 태그, 이미지 라벨). 로더가 역할별 설정·해시를 그대로 넘기고 `SERVING_CONFIG_DIFFERS_BY_ROLE` 거부는 없앴다.
- CR-001은 "스키마 v2 후보 목록"으로 보류(적용 안 함), CR-002는 닫음, CR-003 유지.
- 시험: `.venv/bin/python -m pytest` 168 통과(골든 대조 포함, 1220쌍 중 1215 일치, 5건 (c)). 스모크 SMOKE OK(아래 커밋 뒤 다시 돌림).
- 남은 일: model-runtime v0.3.0 맞춤(헤드 알림 대기), 실제 I4·I6 통합, 실제 백엔드, O06 이후 어댑터.

## 후속 구간 2 (D49·D51)

- 의존 태그: boundary-contracts v1.2.0, model-runtime v0.4.0, turn-engine v0.2.0 (requirements, wheel 빌드, 이미지 라벨). 라벨은 `io.openit.*` 6개.
- I4·I6를 HTTP 클라이언트(`HttpReleaseReader`, `HttpTraceIngest`)로 연결(`SURVEY_RELEASE_READER=http`, URL과 토큰 환경 변수 이름 설정). 번들은 `dest/<릴리스 ID>`.
- 통합 시험: 참조 서버(`tests/ref_servers.py`, boundary-contracts conformance/servers.py 복사)로 번들 수신·적재, 포인터 전환·롤백, UNAVAILABLE/INTEGRITY_ERROR 처리, Trace 전송(재시도·DUPLICATE·DEAD·ID 변환). 스모크도 이미지 안에서 같은 HTTP 경로를 쓴다.
- `pytest` 180 통과, `scripts/smoke.sh` SMOKE OK.
- 한계: 실제 release-gate·ops-feedback 서버와의 E2E는 deploy 몫. model-runtime의 vLLM 백엔드와 어댑터 사전 검사는 서비스에 아직 연결하지 않았다(`SURVEY_MODEL_BACKEND`는 fake만).

## 후속 구간 3 (D55 공개 API)

- 공개 API 구현(`survey_service/api.py`), 계약 v1.3.0으로 상향(requirements, wheel, 라벨), 유휴 만료, 추론 HTTP 경계(`remote.py`, `inference_server.py`).
- `pytest` 213 통과. `scripts/smoke.sh` SMOKE OK(공개 API 한 턴: 401, 시작, 발화, 같은 turn_id 재시도 동일 응답, 404, 종료, 409 포함).
- 한계/남은 일: 직원 개입 결정을 받는 경로 없음(CR-009, 운영 전 필요), 추론 컨테이너는 fake 백엔드로만 시험(별도 컨테이너로 띄운 스모크는 하지 않음), 실제 모델(RTX 4090)·vLLM 연결 안 함, TLS·토큰 회전은 배포 담당.

## 후속 구간 4 (D58)

- 직원 개입 경로 구현, boundary-contracts v1.3.1로 상향. CR-009 해석 1~8 승인됨(401/403 유지).
- `pytest` 228 통과. 스모크에 멈춘 세션 재개·직원 종료·역할 분리·멱등·409 포함.
- 남은 일: 직원 콘솔 쪽 호출자(직원 토큰 전달은 배포 담당), 추론 컨테이너 분리 스모크, 실제 모델 연결.

## 후속 구간 5 (D62 모델 서빙 연결)

- 서비스 안의 추론 서버를 서비스 패키지에서 빼고 시험 대역(`tests/ref_inference_server.py`)으로 옮겼다. 서비스는 `SURVEY_INFERENCE_URL`로 HTTP 호출만 한다. 규약은 `docs/inference-server-spec.md`, 요청은 CR-010.
- 호출 실패·지연·잘못된 출력 처리(타임아웃 4초, 연결 실패·5xx 1회 재시도, 읽기 타임아웃·400·잘못된 출력은 재시도 없음, 404 재적재, 202 적재 폴링)와 시험(`tests/test_inference_client.py`).
- `scripts/smoke_compose.sh`: 서비스 + 가짜 추론 + 가짜 게이트 compose 스모크(추론 중단·복구 포함).

## 후속 구간 6 (D62 LangGraph 전환, 추론 서버 I9 정렬)

- 오케스트레이터를 LangGraph로 전환(설계 14절). 회귀: `pytest` 전체 통과(골든 1220쌍 중 1215 일치, 5건 (c)), 스모크 SMOKE OK, compose 스모크 SMOKE(compose) OK.
- 추론 클라이언트·시험 대역을 HTTP.md I9 절에 맞춤: `X-Openit-Protections` 규칙별 건수를 `/readyz`의 `inference.protections`로 노출, `/v1/info`의 `serving_config_hash`.
- 남은 일: model-runtime의 실제 서버 이미지가 I9 절에 맞게 나오면 compose의 `inference` 이미지 교체, 실제 모델 연결, CR-011(라벨) 회신.

- CR-011 승인(D63). model-runtime 서버가 I9에 맞춰지면 compose의 추론 이미지를 실제 서버로 바꾼다(헤드가 다시 부른다). 지금은 대기.

## 후속 구간 7 (D64 직원 알림, D66 공개 API 보완)

- 구현은 design 15절. 의존: boundary-contracts v1.3.2, pywebpush 2.5.0(잠금 고정).
- 알려진 한계: SSE 연결 생존은 쓰기 실패로만 알 수 있어 연결이 소리 없이 끊기면 다음 ping 전까지 DELIVERED로 볼 수 있다(운영 절차 필요). 알림 저장과 세션 쓰기 확정은 서로 다른 파일이라 한 트랜잭션이 아니다. 실제 브라우저·실제 푸시 서비스(FCM 등)로는 시험하지 못했다(로컬 가짜 푸시 서비스로 전송 형식만 확인).
- 시험: pytest 322 통과, scripts/smoke.sh SMOKE OK(직원 알림 한 바퀴 포함), scripts/smoke_compose.sh OK. 오케스트레이터 그래프(graph.py)는 바꾸지 않았다.

## fix/loader-retry

- 서버에서 실제 vLLM 추론 서버가 뜨기 전에 활성 포인터 적재를 시도해 REMOTE_UNAVAILABLE이 영구 거부로 남던 결함 수정(설계 5절, 런북). main에는 합치지 않았다(서버 검증 뒤 합친다).

## 후속 구간 8 (계약 2.0.0 이전, D74·D75·D76, 브랜치 migrate-contracts-2.0.0)

- 한 일: 서비스를 D74 구조로 바꿨다(design 16~19절). `extract`+`validate_extraction` 노드, 시작은 B만, 1회 재시도(D75), 문항 순서·건너뛰기 표를 문진 스키마로(D76), 표 찾기 초안(ask 틀·칸 설명, 57행 처리), Trace 키 교체, 직원 알림 입력원 비활성 명시(CR-012), 의존 태그 v2.0.0 / v0.3.0 / v0.6.0(당시 값. 현재 기준은 이 절 끝의 "현재 기준" 참조).
- 시험: `.venv/bin/python -m pytest` 전부 통과(수는 보고서 참조). `scripts/smoke.sh` SMOKE OK, `scripts/smoke_compose.sh` SMOKE(compose) OK(docker 데몬 있음, 이 PC 한 환경에서만 확인).
- 확인하지 못한 것: 실제 model-runtime 0.6.0 서버 이미지와의 E2E(계약 참조 서버로만 시험), 실제 모델이 57행 문제의 새 `next.text`를 어떻게 다루는지, 두 번째 깨끗한 환경에서의 재현(이 PC 한 곳).
- 미정(헤드 결정 필요): 직원 알림 입력원과 반복 물음 상한(CR-012), 같은 정수 버전 안의 비호환 스키마 변경을 어떻게 낼지(정수 버전 2 여부, model-runtime이 1만 받는다), 표 초안의 사용자 확인.
- 푸시·태그는 하지 않았다.

## 후속 구간 8 (model-runtime v0.6.1 맞춤, 브랜치 follow-model-runtime-0.6.1)

- 의존·라벨·기대값을 v0.6.1 / prompt-4로. prompt-3 릴리스는 적재하고 경고만. val 대조 결과는 design 17절 끝: A 536/549 글자 일치(불일치 13행은 state 묶음 순서, 원인 미확인), B next.text 제외 549/549.
- 시험: pytest 403 통과, smoke.sh OK, smoke_compose.sh OK (이 PC 한 곳).
- 남은 일: A state 묶음 순서 13행의 원인(학습 데이터 생성 규칙 확인 필요), 두 번째 환경 확인.
- **현재 기준 (2026-10-27 확인, 위 기록은 당시 값)**: 의존 태그는 `requirements.txt` 기준 boundary-contracts v2.0.2 / turn-engine v0.4.0 / model-runtime v0.7.1. 시험은 `pytest --collect-only`로 수집 415개. 403은 당시 통과 수였고 415개의 통과 여부는 이번에 돌리지 않아 미확인. smoke 결과도 이후 갱신 여부 미확인.

## 후속 구간 9 (직원 세션 조회, 위험 규칙 엔진, 위반 관측, 조사)

- 한 일: 직원 세션 조회 `GET /v1/staff/sessions/{id}`(`StaffSessionView` 스키마 추가, 호출마다 접근 기록, CR-013), 위험 규칙 파일 입력원 `SURVEY_RISK_RULES_FILE`(기본 파일 없음, 오류는 시작 거부, `/readyz`에 version·sha256·규칙 개수), 의미 검증 위반 코드의 로그·`/readyz` 카운터, 추론 타임아웃 분류 정정(원인: 추론 타임아웃 기본 4초 + 시간만 보던 분류), 결과 전달 대상 없음을 오류가 아닌 상태로. 자세한 이유는 CHANGELOG.
- 시험: `.venv/bin/python -m pytest -o addopts=""` 448 통과(이전 415). `bash scripts/smoke.sh` SMOKE OK (이 PC 한 환경). `smoke_compose.sh`는 돌리지 않았다.
- 확인하지 못한 것: 5.65초 timeout이 실제로 어느 경우(읽기 타임아웃, 느린 5xx, 재적재 대기)였는지(당시 로그에 reason 없음, gpu-dev 접속 금지), 프런트가 `StaffSessionView`의 null 허용(`created_at`·`turn_id`·`at`)과 `values[].status` 값을 받아들이는지, 두 번째 깨끗한 환경에서의 재현.
- 한계: 위험 규칙은 A 출력 검증을 통과한 턴에서만 평가된다. 알림 DB(`alerts.db`)가 조회의 시각·접근 기록 원본이 되었다(PostgreSQL 이전 때 함께 옮겨야 한다).
- 미정(헤드): CR-013(PUBLIC_API.md 반영), 정수 버전은 올리지 않았다(근거 CHANGELOG), 규칙을 검증 앞에서 평가할지.
- 푸시·태그는 하지 않았다.

## 후속 구간 10 (D85 1단계)

- 한 일: subject_ref(A), 결과 조회(B), StaffSessionView 보강(C), 외부 위험 신호(D), 직원 식별 헤더(E), 세션 보존 잡. 자세한 내용과 표준 이유는 CHANGELOG.
- 시험: `pytest -o addopts=""` 466 통과, `scripts/smoke.sh` SMOKE OK(이 PC 한 환경). 확인하지 못한 것: 실제 게이트웨이를 거친 `X-Openit-User` 전달, 프런트·병원 시스템과의 실제 호출.
- 다음: 2단계(PostgreSQL).

## 후속 구간 11 (D85 2단계: PostgreSQL)

- 한 일: 세션 저장소·outbox·알림 DB(알림, 시각, 접근 기록, 세션 메타, 위험 신호, 푸시 구독)에 PostgreSQL 구현. `SURVEY_DATABASE_URL`이 있으면 PostgreSQL, 없으면 SQLite. 끊긴 연결 재접속, 접속 문자열 비노출, `/readyz`의 `database.kind`. 이유와 한계는 CHANGELOG.
- 시험: `TEST_DATABASE_URL`(`../deploy/scripts/dev-postgres.sh up survey`, 일회용 PostgreSQL 16)를 주고 `pytest -o addopts=""`: 502 통과(SKIP 0). 같은 구성의 앞선 한 번의 실행에서 `test_webpush_sender_against_local_push_service[410-GONE]`가 한 번 실패했고(PostgreSQL과 무관한 로컬 푸시 서비스 시험, 3번 따로 돌리면 통과, 타이밍 의심, 원인 미확인) 최종 실행은 통과. 없이 돌리면 472 통과 + 30 SKIP. `scripts/smoke.sh` SMOKE OK, `SMOKE_PG=1`도 OK(이 PC 한 환경). deploy `check-all.sh` FAIL 0.
- 확인하지 못한 것: 운영 compose에서 `db-survey-dsn` 비밀을 마운트한 실제 서비스 기동(deploy 쪽 일), PostgreSQL 서버 재시작(컨테이너 재시작) 상황(연결 강제 종료로만 시험), 두 번째 깨끗한 환경.

## 후속 구간 12 (B 입력 표 찾기를 데이터셋 담당의 labels.py 규칙으로 교체)

- 한 일: 역으로 읽은 초안 표를 데이터셋 담당의 원본 규칙(학습 서버 labels.py, 2026-10-14)을 옮긴 표 찾기로 교체. 이유와 바뀐 동작은 CHANGELOG, 설명은 design 18절.
- 확인한 것: 학습 데이터 B 입력 5,077/5,077행(START 제외, train 3,982 + val 549 + test 546) 글자 단위 일치, START 110/110, 원본 코드와 조합 비교(ask 20,332, value 1,134, 무작위 b_input 40,000) 전부 일치.
- 확인하지 못한 것: 데이터셋 담당의 표 승인(기록 미확인, 확인 대기), 실제 문항 세트 문장으로 만든 `next.text`와 모델의 결합, B 모델이 57행 문제의 남은 칸 문장을 어떻게 다루는지.
- 시험 수와 스모크는 이 절 아래 보고 참조가 아니라 CHANGELOG의 시험 항목과 커밋 메시지를 본다(E2E 수는 쓰지 않는다).

## 2026-10-27 /readyz의 PostgreSQL 장애 응답 (CR-D23)

- 한 일: `/readyz`가 DB 장애 때 `SURVEY_READYZ_DB_TIMEOUT_SECONDS`(기본 2초)+약 2초 안에 503(`database.status=UNAVAILABLE`, `detail=db unavailable`)으로 답하고 복구 뒤 200이 되게 했다. 별도 짧은 연결(`PgConn.probe`)로 세 저장소를 동시에 확인하고 하드 상한을 두며, DB를 읽는 보조 수치 예외로 연결이 끊기던 것을 막았다. 원인·측정은 CHANGELOG 맨 위 절, 경계 문서 §5·§6.
- 확인한 것(이 PC, 로컬 PostgreSQL 컨테이너): 고치기 전 연결 불가에서 세 확인 30.0초, `docker pause`에서 120초 넘게 무응답. 고친 뒤 전체 pytest(PostgreSQL 붙임) 990 통과, 새 시험 4개(`tests/test_readyz_db_outage.py`), `scripts/smoke.sh` SMOKE OK.
- 확인하지 못한 것: 고친 뒤 응답 시간의 숫자 재측정(시험이 6초 미만만 검증), 실제 compose 환경의 재측정(deploy E2E 5p에서 헤드), RemoteDisconnected의 직접 원인이 보조 수치 예외라는 것은 코드 읽기로 본 추정, `SMOKE_PG=1 smoke.sh`는 돌리지 않음.
- 남은 일/제안: 응답 없는 DB에서 요청 경로가 최대 10초(`connect_timeout`) 기다리는 점은 그대로다. 단축은 환자 경로 의미가 바뀌므로 별도 결정.
