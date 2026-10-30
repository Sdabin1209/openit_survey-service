# survey-service (문진 서비스)

환자의 발화 한 턴을 받아 저장할 값으로 바꾸고, 다음에 할 말을 돌려주는 HTTP 서비스.

환자(키오스크 프론트)가 말하면 서비스가 세션을 만들거나 이어 받고, 추론 서버의 모델(LoRA-A)에게 값 추출을 시키고, 그 결과가 문항 규칙에 맞는지 검증해 저장한 뒤, 문장 틀 표를 찾아 모델(LoRA-B)이 응답 문장을 만들게 한다. 모든 턴의 기록(Trace)은 ops-feedback으로 보낸다. 같은 되묻기가 끝없이 반복되지 않게 하는 상한 장치와, 위험할 때 직원을 부르는 알림 길도 이 영역에 있다. **하지 않는 것**: 값의 정규화·판단(모델이 한다, 서비스는 검증만 한다), 모델 로딩·프롬프트(model-runtime), 문항·모델 승인(release-gate), 화면(프론트), 직원 콘솔 화면(docs/brief.md §1, docs/design.md §16).

## 전체 속의 위치

| 방향 | 이웃 | 인터페이스 | 내용 |
|---|---|---|---|
| 받음 | Frontend(키오스크) | I10 공개 API: `POST /v1/sessions`, `/v1/sessions/{id}/turns`, `/end`, `GET /v1/sessions/{id}/state` | 세션 시작, 발화, 종료. `turn_id`로 멱등 |
| 받음 | 직원 콘솔 | I10: `POST /v1/sessions/{id}/intervention`, `GET /v1/staff/alerts`, `/v1/staff/alerts/stream`(SSE), 푸시 구독 | 직원 개입(재개·종료), 알림 조회 |
| 받음 | release-gate | I4 ReleaseReader(HTTP 클라이언트) | 활성 포인터 폴링, 승인된 릴리스 묶음 적재 |
| 부름 | 추론 서버(model-runtime) | I9: `PUT /v1/releases/{id}`, `POST .../extract`, `POST .../respond` | LoRA-A 추출, LoRA-B 응답 |
| 보냄 | ops-feedback | I6 TraceIngest `send(TraceEnvelope)` | 턴마다 한 건, 영속 outbox 경유, 최소 1회 |

출처: docs/boundary.md §2~3, docs/design.md §1, `survey_service/api.py`. 이웃의 SDK는 이 영역이 제공하지 않는다(SDK 인터페이스 없음, docs/brief.md §2).

## 구조

구조 규칙은 head/docs/DESIGN.md(D117). 컴포넌트 표는 [docs/components.md](docs/components.md), 내부 타입은 `schemas/`([스키마 밖 조건](schemas/README.md)).

| 경로 | 역할 |
|---|---|
| `survey_service/components/` | 컴포넌트(파일 하나 = 업무 하나). `link_*`는 이웃 통신(받는 쪽 `link_public_api`, 보내는 쪽 `link_inference` `link_push` `link_trace_export` `link_boundary_clients`) |
| `survey_service/wiring/` | 한 턴의 순서(`turn_graph.py` LangGraph 노드), `orchestrator.py`, 조립 `app.py`, 저장소 선택 `db.py`, 공용 기반(`errors` `ports` `pg` `netguard`) |
| `survey_service/types/` | `schemas/`에서 생성한 타입과 스키마 검증(손으로 고치지 않는다, 4절) |
| `survey_service/server.py`, `settings.py`, `cli.py`, `migrate.py`, `migrations/` | 진입점, 환경 변수 설정, 마이그레이션(업무 규칙 없음) |
| `schemas/` | 영역 내부 타입의 JSON Schema와 통과·거부 예시 |
| `schema/` | 문진 스키마(이 영역이 소유, 정수 버전 1). 문항 흐름·표 찾기 표 데이터 포함 |
| `tests/` | 시험(`test_structure.py`가 구조 검사)과 이웃 대역 |
| `scripts/` | `build-image.sh`, `smoke.sh`, `gen-types.sh`, `check_schemas.py` |
| `Dockerfile`, `requirements.txt`, `requirements.lock` | 이미지와 의존 고정 |

한 턴의 흐름(docs/design.md §16):

```mermaid
flowchart LR
  F[프론트 발화] --> L[load_session] --> B[bind_release]
  B --> E[extract: LoRA-A]
  E --> V[validate_extraction]
  V -- 위반 --> E
  V --> P[plan_writes] --> C[commit]
  C --> S[deliver_staff] --> R[respond: 표 찾기 + LoRA-B] --> T[Trace outbox]
```

위반이면 같은 요청을 1회만 재시도하고(D75), 그래도 틀리면 턴 실패(`NOT_COMMITTED`)로 세션을 바꾸지 않는다. 시작·직원 재개/종료·이탈 턴은 A를 부르지 않는다.

## 실행 방법

전제: Python 3.10 이상, git과 GitHub ssh 접근(이웃 라이브러리 boundary-contracts, model-runtime은 `requirements.txt`의 Git 태그에서 설치하고 옆 폴더를 쓰지 않는다), 스모크에는 Docker 데몬 접근 권한. 환경 준비의 정본은 `scripts/setup-env.sh`다(멱등, 어느 폴더에서나 실행, 오래된 .venv도 핀과 같게 맞춘다).

```bash
# 1) 설치
bash scripts/setup-env.sh
# 2) 시험
.venv/bin/python -m pytest
# 3) 이미지 스모크 (빌드 + /healthz /readyz + 비root + 라벨 + 한 턴)
bash scripts/smoke.sh
# 4) 서비스 + 가짜 추론 + 가짜 게이트 compose 스모크
bash scripts/smoke_compose.sh
# 5) 실행: 필수 환경 변수 3개(없으면 종료 코드 2)
SURVEY_RELEASE_READER=http SURVEY_TRACE_INGEST=http SURVEY_MODEL_BACKEND=fake python -m survey_service
```

비밀(API 토큰, VAPID 키 등)은 값을 환경 변수에 직접 넣지 않고 `NAME_FILE`(파일 경로)로 받는다(D84, docs/runbook.md §1). 이름 목록과 나머지 설정은 runbook §1. 실제 운영 실행에는 release-gate·ops-feedback·추론 서버 URL과 토큰 파일이 더 필요하다(설정 전체는 runbook 참조, 이 문서는 개발용 최소만 적었다).

시험 개수와 통과 결과는 아래 '현재 상태'에만 적는다. 변경 때마다 `pytest` 실제 출력(위 2)의 명령)으로 다시 확인한다. 명령은 문서(runbook, scripts)에서 옮겼다.

## 4. 타입 생성 (schemas/ -> types/)

`survey_service/types/`는 `schemas/*.json`에서만 만든다. 손으로 고치지 않는다. 도구(datamodel-code-generator 0.87.0, `requirements-tools.txt`에 고정)는 런타임 의존이 아니라 별도 가상환경에 설치된다.

```bash
bash scripts/gen-types.sh                       # types/ 를 다시 생성한다(.venv-tools는 setup-env.sh가 만든다)
.venv/bin/python scripts/check_schemas.py       # 모든 스키마의 통과·거부 예시를 확인한다
```

`tests/test_structure.py` 5번이 임시 폴더에 다시 생성해 `types/`와 같은지 본다.

## 현재 상태

- 시험 상태(2026-10-28, 커밋 ce2fb4d, 워크스페이스 밖 새 폴더에 로컬 `git clone`해 이웃 폴더 없이 `bash scripts/setup-env.sh`를 두 번 실행한 뒤 측정, Linux 6.6 WSL2 · Python 3.12.3, 같은 PC): `.venv/bin/python -m pytest` 출력 `1159 passed, 54 skipped in 156.23s`(PostgreSQL 시험은 `TEST_DATABASE_URL`이 없어 SKIP). 이 숫자는 여기에만 적는다(`docs/STATUS.md`는 이전 시점 기록). 다른 머신(GitHub 원격에서 clone)에서는 확인하지 못했다.
- 확인하지 못한 것(STATUS, OPEN_GAPS): 실제 model-runtime 서버 이미지와의 E2E, 다른 머신에서의 재현, 실제 브라우저·푸시 서비스, 실제 모델로 한 턴을 통과시키는 일(U07), 부하·동시성, 세션 문서 암호화·보존 기간.
- 표 찾기는 데이터셋 담당의 규칙(학습 서버 labels.py, 2026-10-14)을 옮긴 것이고 확인 대기다(`schema/data/response_tables.json`의 `table_status: DATASET_RULES_UNCONFIRMED`, 승인 기록 미확인).
- `/readyz`는 PostgreSQL이 죽거나 응답이 없어도 `SURVEY_READYZ_DB_TIMEOUT_SECONDS`(기본값은 `survey_service/settings.py`)가 정하는 총 마감 안에 503(db unavailable)으로 답하고 복구 뒤 200이 된다(CR-D23, docs/boundary.md §5·§6). 이 PC에서 로컬 PostgreSQL 컨테이너로 확인했고, 실제 compose 환경의 재측정은 deploy E2E 5p에서 헤드가 한다.
- 직원 알림은 기본으로 임시 규칙 목록(임상 검토 전)이 모든 발화 턴에서 돌아 만들어진다(`/readyz`의 `staff_alert_source.active=true, provisional=true`, `SURVEY_RISK_DEFAULT_RULES=off`이면 꺼짐, docs/boundary.md §5).

## 이 영역의 열린 항목 (head/docs/OPEN_GAPS.md, 읽기 전용으로 확인)

| ID | 요약 | 막는 것 |
|---|---|---|
| G01 | 직원 알림의 임상 입력원이 확정되지 않았다. 지금은 임상 검토 전 임시 기본 규칙 목록이 켜져 있고(`provisional`), 병원 규칙 파일·포트(`SURVEY_RISK_SOURCE`)로 바꾼다. 환자 안전과 닿는다 | 사용자 결정(임상 검토) |
| G12 | 재질문 상한: 기본 3(`SURVEY_MAX_CONSECUTIVE_REASK`, 0이면 끔, ADR-0020)으로 구현됨. 값은 측정하지 않은 헤드 결정이고, 정지 이름 `PAUSED_RISK` 재사용으로 키오스크가 위험 정지와 구분하지 못한다(변경 요청 필요 여부는 헤드 판단) | 헤드 판단 대기(계약) |
| G03 | 표 찾기는 데이터셋 담당의 labels.py 규칙을 옮김, 표 승인 기록 미확인(IO_SPEC 7장 2번 미정) | 다른 영역(데이터셋 담당 확인) |
| G13 | 표 `DATASET_RULES_UNCONFIRMED`, 57행 문제용 `next.text`를 B 모델이 어떻게 다루는지 미확인, A 입력 state 묶음 순서 13/549행 불일치 | 다른 영역(데이터셋)·GPU(모델 확인) |
| G11 | 정수 버전 1 안에서 비호환 변경. ops-feedback과 같은 잠금으로 함께 배포해야 한다 | 다른 영역(ops-feedback), 사용자 결정 |
| U07 | 실제 LLM 데모에서 학습 안 된 모델이 없는 slot key를 지어내 14턴 모두 실패 | GPU·학습 쪽 |
| U14 | 제약 디코딩이 slot key를 현재 문항 범위로 제한하지 못함(계약 변경 필요) | 헤드 판단 대기(계약), 다른 영역 |
| D-E | 회귀 평가가 읽는 v2 TurnTrace 입력 키 이름이 추정(이 영역 확정 후) | 다른 영역(평가 쪽)이 이 영역의 확정을 기다림 |

G07은 닫힘이라 뺐다. 위 요약은 대장의 한 줄을 줄인 것이며 원문 상태는 대장이 기준이다.

## 설계 결정 기록 (ADR)

구조·기본값 선택의 '왜'는 [docs/adr/](docs/adr/README.md)에 있다(SQLite와 PostgreSQL, 연결 풀 없음, ThreadingHTTPServer, LangGraph, 문서 저장, 멱등, 턴 마감 timeout 체인, outbox, SSE, 위험 규칙 엔진 위치, 보존 기본값, 기기별 토큰, 값 검증, HTTP 방어).

## 더 자세한 문서

| 문서 | 내용 |
|---|---|
| `CLAUDE.md` | 이 리포 에이전트 지침 |
| `docs/brief.md` | 영역 브리프: 목적, 범위, 수용 기준, 건드리면 안 되는 경계 |
| `docs/design.md` | 내부 설계서(한 턴의 흐름은 16절) |
| `docs/boundary.md` | 소유 저장소, 제공·소비 계약과 버전, 이웃에게 지키는 것, 장애 영향 |
| `docs/runbook.md` | 환경 변수, 상태 보기, 실패 대처, 이미지, 스모크 |
| `docs/STATUS.md` | 구간별 한 일, 시험 결과, 한계 |
| `docs/CHANGELOG.md` | 변경 이력 |
| `docs/inference-server-spec.md` | 추론 서버 HTTP 규약 |
| `docs/change-requests/` | 헤드에게 보낸 변경 요청(CR-001~016, 목록은 docs/change-requests/) |
| `docs/rules/` | 헤드가 전달한 업무 규칙과 입력 조립 표 |
