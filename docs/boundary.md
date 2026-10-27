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
| I9 model-runtime | **v0.7.2** (같은 규칙) HTTP 서버 `PUT /v1/releases/{id}`, `POST .../extract`(JSON), `POST .../respond`(text/plain). 매니페스트 기대값: `model_runtime_version` 0.7.x, 역할별 `system_prompt`, serving_config `prompt-4`(D77)·`protect-2`. `prompt-3`은 v0.7.0에서 제거되어(`prompt_template_versions=['prompt-4']`) 적재되지 않는다. 프로세스 안 `load`는 개발·스모크용 |

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
- **장애 정책 구현 1단계(head/docs/FAULT_POLICY.md 원칙 2·3·4·9)**:
  - 같은 `turn_id`: 확정된 것은 저장된 결과를 그대로 돌려준다(이전부터). **처리 중인 같은 `turn_id`가 겹치면 두 번째는 새로 시작하지 않고 `409`(`code: CONFLICT`, `details.reason: TURN_IN_PROGRESS`) + `Retry-After` 헤더**다. 이전에는 세션 락에서 기다렸다가 저장된 결과를 받았다(값은 두 번 저장되지 않았다). 응답 형식은 기존 규약(HTTP.md의 409 사유는 `details.reason`)을 따르며 CR-015는 처리됐다(PUBLIC_API.md 409 행에 의미 기재). 입력 불일치 409는 `details` 없음. 다른 turn_id의 같은 세션 요청은 이전처럼 락에서 순서대로 처리된다.
  - 서버 턴 총 마감 `SURVEY_TURN_DEADLINE_SECONDS`(기본 **6초**, 근거는 아래 불변식과 측정). 요청을 받은 때부터(API의 사전 확인 포함) 세션 락 대기·멱등 확인·모델 호출(재시도 포함)·DB 대기·쓰기 확정 전체에 건다. 넘으면 `turn_failure_signal.write_committed=NOT_COMMITTED`(`staff_alert=NONE`)로 응답하고 결과를 고정하지 않는다(같은 turn_id 재전송은 새로 처리된다). 처리는 데몬 스레드에서 돌고 마감 뒤에는 모델을 더 부르지 않으며 **쓰기 확정을 시작하지 않는다**. 한계: 마감 직전에 이미 시작된 확정은 뒤늦게 반영될 수 있고, 그러면 재전송은 기존 경로(쓰기는 확정, 결과 없음 신호)로 그 사실을 알린다. 마감 초과 건수는 카운터 `turn_deadline_exceeded`. **위험 신호가 있는 턴의 확정 실패 시 알림 전송 여부는 사용자 결정 대기라 바꾸지 않았다**(확정 실패 턴은 지금처럼 알림이 나가지 않는다).
  - DB 주 연결 상한: `SURVEY_DB_CONNECT_TIMEOUT_SECONDS`(기본 2, 이전 고정값 10)와 `SURVEY_DB_STATEMENT_TIMEOUT_SECONDS`(기본 2). 문장 하나는 서버 `statement_timeout`으로, 서버가 얼어 응답이 없으면(예: `docker pause`) 서버 쪽 값은 소용없으므로 **상한+1초 뒤 클라이언트가 소켓을 닫아** 끝낸다. 근거: 정상일 때 문장은 수 ms이고 턴 쓰기는 몇 문장이라 2초면 느린 디스크·잠깐의 잠금은 견디고, libpq의 접속 상한은 2초 미만이 의미 없다. **의미가 바뀐 점**: 재접속·재시도 규칙(트랜잭션 밖 멱등 한 문장만 한 번 재시도, 트랜잭션 도중 끊김은 재시도 없음)은 그대로이고 접속 대기가 10초에서 3초로 줄었다. 시작 때 스키마 생성은 이 상한을 받지 않는다(다른 인스턴스의 잠금을 기다릴 수 있음).
  - Trace 인증 실패 등으로 DEAD가 된 건수는 `/readyz`의 `trace_outbox.dead`와 원인 코드별 `trace_outbox.dead_by_code`(예: `PERMISSION_DENIED`)로 나온다. `/readyz`를 503으로 만들지는 않는다(수집 도달은 준비 조건이 아니다). 경보로 쓰는 쪽은 `dead_by_code`가 0이 아닌 것을 보면 된다.
  - **빠른 실패와 시간 불변식(DB 중지·무응답)**: 접속 실패 뒤 `SURVEY_DB_RECONNECT_BACKOFF_SECONDS`(기본 1초) 동안은 같은 연결이 재접속을 시도하지 않고 바로 실패한다(재접속 유예). 이유(측정): 이전에는 턴 하나가 사전 확인·멱등 확인·세션 읽기에서 접속 시도를 연달아 각각 끝까지 기다렸다. 이 PC에서 postgres 컨테이너를 중지했을 때 턴 3.0초(접속 상한 3초), `docker pause`했을 때 10.0초(문장 상한+1초 4.0 + 접속 3.0 + 접속 3.0)였고, 유예와 상한(접속 2, 문장 2)을 적용한 뒤 중지 2.0초(503 UNAVAILABLE), pause 5.0초(503 UNAVAILABLE)다. 의미는 그대로다: 트랜잭션 밖 멱등 한 문장만 새 연결로 한 번 재시도하고, 트랜잭션 도중 끊김은 재시도하지 않는다. 바뀐 것은 접속 실패 직후 1초 동안 재접속을 건너뛰는 것뿐이다(그 사이 DB가 돌아와도 최대 1초 늦게 복구). **불변식: 키오스크 대기 10초 > gateway 9.5초(연결 1 + 응답 헤더 8.5) > 서버 턴 마감(기본 6초) + 응답 전달 여유**. 서버 마감은 gateway 상한보다 2.5초 이상 짧아야 하고, 마감을 바꿀 때 이 순서를 지킨다(시험 `tests/test_turn_db_outage_bounds.py`가 기본값에서 확인). DB가 느리지만 살아 있어 사전 확인이 각 2초씩 쓰더라도 마감은 요청을 받은 때부터 세므로 전체가 마감 안에 든다. 확인하지 못한 것: compose 네트워크에서 postgres 컨테이너를 중지했을 때 접속이 거부되는지 도달 불가로 시간이 걸리는지(이 PC의 단일 호스트 포트에서는 접속 상한만큼 걸렸다). 어느 쪽이든 접속 상한(2초) 안에 끝난다.
  - 확인한 것(이 PC, 로컬 PostgreSQL 컨테이너 + 시험 대역): 위 동작의 시험은 `tests/test_turn_idempotency_deadline.py`, `tests/test_readyz_db_outage.py`(`docker pause` 포함), `tests/test_trace_export.py`. 확인하지 못한 것: 실제 compose 환경·gateway를 거친 경로(FAULT_MATRIX A2 gateway 응답 코드와 키오스크 재시도는 다른 영역), 실제 모델 서버의 느린 응답과의 조합, 키오스크 재시도 예산과의 맞물림.
- **FAULT_MATRIX A6·A8·A10 (장애 정책 원칙 4·9·10·12)**: 확인한 것은 코드를 읽고 `tests/test_fault_a6_a8_a10.py`로 시험한 범위다.
  - **A6 Trace outbox -> 수집**: (F2 멈춤) 수집이 응답하지 않으면 전송 스레드만 HTTP 시간 초과(`SURVEY_HTTP_TIMEOUT_SECONDS`, 기본 10초)까지 기다리고 UNAVAILABLE로 지수 백오프(1~60초)한다. 턴은 outbox에 넣기만 하므로 영향이 없고 행은 PENDING으로 남는다(유실 없음). (F5 5xx) 같은 처리. (F6 깨진 응답) 이전에는 200인데 본문이 깨진 응답이 INTEGRITY_ERROR로 곧바로 DEAD(유실)가 됐다. **고침**: 5xx와 같이 실패로 보고 `max_unexpected_attempts`(20)까지 백오프로 다시 보낸 뒤에만 DEAD(수집이 (세션, 턴)으로 멱등이라 안전). **큐 상한**: 이전에는 수집이 오래 멈추면 outbox가 무한히 쌓였다. **고침**: `SURVEY_OUTBOX_MAX_PENDING`(기본 20000, 0이면 끔)에 닿으면 새 Trace는 넣지 않고(턴은 정상, 기존 `trace_enqueue_failed`) `/readyz`의 `trace_outbox.dropped_full`로 건수를 낸다. 상한 근거: 봉투 하나가 수 KB라 약 100MB 안팎(추정, 실측 아님). 상한에 닿는 것 자체가 경보 대상이다(`pending`·`oldest_pending_age_seconds`·`dropped_full`).
  - **A8 survey-data 볼륨의 releases/ 사본**: 사본은 서비스가 읽어 쓰는 캐시가 아니다. 적재 때마다 이웃(I4)에서 번들을 받아 해시를 모두 검증한 뒤 교체하므로 (F6 파일 손상) 손상된 사본은 신뢰하지 않고 덮어쓴다(시험으로 확인). 문항 세트는 메모리에 있다. 결과로 이웃(I4)이 죽어 있으면 서비스를 다시 띄운 직후 사본이 있어도 적재하지 못한다(`release_loading`, 알려진 한계). (F1 볼륨 쓰기 불가) 이전에는 `OSError`가 폴링 스레드에서 일반 로그만 남기고 재시도 상태도 `/readyz` 사유도 없었다. **고침**: `STORAGE_UNAVAILABLE`(일시 실패)로 백오프 재시도하고 `/readyz`의 `release_loader.retrying`과 `release_loading`으로 드러난다(오류 메시지는 예외 클래스 이름만). 이전 릴리스는 유지.
  - **A10 직원 SSE**: 알림은 DB에 영속하고 목록 API(`GET /v1/staff/alerts?status=OPEN`)로 언제든 따라잡을 수 있다. 재개 수단은 `Last-Event-ID`(이벤트 순번, 재시작 뒤에도 이어짐, 시험 있음). **고침**: (1) 이어 받을 수 없는 재연결(지워진 구간을 건너뛰었거나 순번이 앞섬)에는 `event: resync`(`reason: EVENTS_NOT_RESUMABLE`)를 보내 클라이언트가 목록 API로 다시 맞추게 한다. (2) 알림 DB가 죽었을 때 이전에는 핸들러 예외로 연결이 끊겼다. 이제 스트림을 열기 전이면 `503 UNAVAILABLE` + `Retry-After`, 열린 뒤면 스트림을 정상 종료한다(클라이언트는 `retry: 3000`으로 재연결). (3) `alert_events`는 지우는 코드가 없어 무한히 늘었다. 이제 주기 작업이 최근 1만 건만 남긴다(알림 행은 남는다). (4) 스레드·구독 누수: 조용히 떠난 클라이언트는 하트비트 쓰기 실패로 감지되어 구독과 스레드가 정리된다(시험: 8개 연결을 끊고 5초 안에 0). 재시작·SIGTERM 때 스트림은 0.5초 안에 끝난다. (5) **이름 있는 하트비트**: 브라우저 `EventSource`는 `: ping` 주석 줄을 앱 코드에 보여 주지 않아 조용히 죽은 스트림을 감지할 수 없다(kiosk-frontend CR-004). 연결 직후와 `SURVEY_SSE_HEARTBEAT_SECONDS`(기본 15초)마다 `event: heartbeat`, `data: {"ts": ...}`(id 없음)를 보낸다(`: ping`도 유지). 스트림 스레드는 하트비트에 DB를 읽지 않아 알림 DB 장애·락 점유 중에도 계속 보낸다(시험 있음). 이 이벤트 이름과 `resync`는 PUBLIC_API.md에 없는 추가라 CR-016으로 제안했고, 기존 클라이언트가 무시하는 하위 호환 형태다. 한계: 동시 SSE 연결 수 상한이 없다(직원 토큰이 있어야 연결 가능, 연결마다 스레드 하나). gateway·브라우저 쪽 재연결 동작(F8 분리 포함)은 이 영역 밖이라 확인하지 못했다.
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
| 같은 turn_id가 처리 중에 다시 옴(프론트 10초 중단 뒤 재전송, N4) | `409` `CONFLICT` + `details.reason=TURN_IN_PROGRESS` + `Retry-After`, 카운터 `turn_in_progress` | 새로 처리하지 않는다(값 이중 저장 없음) | 키오스크가 같은 turn_id로 재시도 |
| 턴이 총 마감(6초)을 넘음(느린 모델·DB) | `NOT_COMMITTED`, 카운터 `turn_deadline_exceeded` | 그 턴은 반영되지 않는다(마감 직전에 시작된 확정은 뒤늦게 반영될 수 있음, 한계) | 같은 turn_id 재전송 |
| 수집 서버 멈춤·5xx·깨진 응답(A6) | `trace_outbox.pending`·`oldest_pending_age_seconds` 증가, 상한이면 `dropped_full` | 턴 영향 없음. 상한까지는 유실 없이 쌓이고 복구 후 재전송, 상한을 넘기면 새 Trace 유실 | 수집 복구 후 자동 |
| 릴리스 볼륨 쓰기 불가(A8 F1) | `release_loader.retrying`의 `STORAGE_UNAVAILABLE`, `release_loading` | 새 릴리스가 안 켜진다(이전 유지) | 볼륨 복구 후 자동 |
| 알림 DB 장애 중 직원 SSE(A10) | 스트림 `503` + `Retry-After` 또는 정상 종료 | 직원 화면이 재연결을 반복. 복구 후 `Last-Event-ID`로 이어 받거나 `resync` 뒤 목록 API | 자동 |
| Trace 인증 실패(A6 F7) | `/readyz` `trace_outbox.dead_by_code.PERMISSION_DENIED` | Trace 유실(턴은 정상), 재시도하지 않는다 | 토큰 수정 뒤 DEAD 건은 수동 처리(자동 재전송 없음, 미구현) |
| Session Store 오류 | 쓰기 확정 실패 → `NOT_COMMITTED` 신호, 응답 없음. 조회 실패는 로그 없는 실패 신호 | 그 턴은 반영되지 않는다. 확정 뒤의 일은 하지 않는다(위험 알림도 나가지 않는다, 아래 주의) | 저장소 복구 후 새 턴 |
| 직원 콘솔 전달 실패 | `DELIVERY_FAILED` 신호, 응답은 그대로 | 쓰기는 반영됨. 직원이 알림을 받지 못함 | 프론트가 신호로 대처(규칙 J3) |
| 모델 출력이 깨짐 | model-runtime의 출력 보호(대체 출력) | 턴은 실패하지 않음 | model-lab이 `*_raw`로 품질 확인 |
| 컨테이너 재시작 | 진행 중 세션 유지(SQLite), 릴리스 재적재 | 재적재 전까지 `/readyz` 503 | 자동 |

**주의(안전)**: 쓰기 확정이 실패한 턴에서는 위험 신호가 있어도 직원 알림을 보내지 않는다. 규칙(preconditions 0-1.3, J8f)이 그렇게 정한다. 프론트는 `NOT_COMMITTED` 신호를 받으면 사용자에게 다시 말하게 하거나 직원 호출로 대처해야 한다. 이 정책이 위험 상황에서 충분한지는 도메인 소유자가 확인할 일이다.

## 6-1. 비기능 측정: 부하·연결 수·재시작 (FAULT_MATRIX N2·N3·N8)

**측정 환경과 한계(반드시 같이 읽을 것).** 이 PC(Intel Core Ultra 7 258V, 8 논리 CPU, 16GB, WSL2, Python 3.12.3, 로컬 PostgreSQL 16 컨테이너(메모리 데이터), 같은 PC의 루프백)에서 한 프로세스(App + ThreadingHTTPServer + 부하 클라이언트 스레드)로 잰 값이다. **모델은 가짜(FakeBackend)라 실제 LLM 지연은 포함되지 않는다**(`--model-delay`의 고정 지연은 가정값이다). gateway·네트워크·컨테이너 자원 한도도 없다. 임상 현장의 키오스크 동시 대수는 알 수 없어 목표를 정하지 않았고, 아래는 "이 구조가 N=?까지 마감 안에 처리했다"는 측정 사실이다. 스크립트: `scripts/load_test.py`(pytest 수집 대상 아님). 세션마다 발화 3개를 순차로 보내고 N개 세션이 동시에 시작한다. 지연은 클라이언트가 잰 요청 하나의 시간이다. 스레드 수는 부하 클라이언트 스레드(N개)를 포함한다.

| 구성 | N | 요청 | 200 | p50 s | p95 s | max s | 그 밖의 코드 | PG 연결 최대 | 스레드 최대 | RSS MB | 처리량 req/s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 기본(`API_MAX_INFLIGHT=32`) | 1 | 3 | 3 | 0.03 | 0.05 | 0.05 | - | 3 | 9 | 91→92 | 21 |
| | 10 | 30 | 30 | 0.19 | 0.51 | 0.52 | - | 3 | 37 | 93→108 | 27 |
| | 50 | 150 | 76 | 0.37 | 1.79 | 2.66 | 429:50, 404:24(시작이 429라 세션 없음) | 3 | 123 | 108→155 | 27 |
| | 100 | 300 | 51 | 0.12 | 2.08 | 3.54 | 429:187, 404:62 | 3 | 173 | 155→177 | 60 |
| inflight 512 | 1 | 3 | 3 | 0.02 | 0.03 | 0.03 | - | 3 | 9 | 91→92 | 33 |
| | 10 | 30 | 30 | 0.15 | 0.17 | 0.17 | - | 3 | 38 | 92→99 | 49 |
| | 50 | 150 | 150 | 2.80 | 3.73 | 4.06 | - | 3 | 159 | 100→195 | 12 |
| | 100 | 300 | 293 | 5.31 | 8.61 | 9.34 | NOT_COMMITTED:7 | 3 | 309 | 195→252 | 13 |
| inflight 512 + 가짜 모델 지연 0.3초/호출(가정) | 1 | 3 | 3 | 0.78 | 0.79 | 0.79 | - | 3 | 11 | 91→116 | 1.1 |
| | 10 | 30 | 30 | 1.24 | 1.77 | 1.89 | - | 3 | 38 | 117→151 | 6 |
| | 50 | 150 | 150 | 2.96 | 4.62 | 5.88 | - | 3 | 158 | 151→207 | 12 |
| | 100 | 300 | 300 | 4.42 | 7.08 | 7.36 | - | 4 | 309 | 207→264 | 14 |

읽는 법과 확인한 사실:
- **마감 안에 처리한 범위(측정 당시 마감은 8초였다. 이후 기본을 6초로 낮췄고 이 부하 표는 다시 재지 않았다)**: 이 PC·가짜 모델에서 동시 50 세션까지는 모든 요청이 4.1초(가짜 지연 0.3초 가정 시 5.9초) 안에 200으로 끝났다. 동시 100 세션에서는 최대 9.3초(p95 8.6초)로 마감을 넘기는 요청이 생겼고 7건이 NOT_COMMITTED로 끝났다(턴 마감이 작동한 결과이고 같은 turn_id 재전송은 안전하다). 지연 0.3초 가정에서도 100 세션 최대 7.4초로 마감에 근접했다.
- **서비스 기본 동시 처리 한도 32(`SURVEY_API_MAX_INFLIGHT`)**: 이를 넘는 동시 요청은 429(`Retry-After`)로 거절된다. 위 기본 구성의 429가 그것이고, 키오스크가 429를 재시도하는 설계다. 이 한도 덕분에 처리 중인 요청의 지연은 N=100에서도 최대 3.5초였다(대신 성공률이 낮다: 이것은 부하를 막는 정상 동작이다).
- **연결 수·풀(N3)**: PostgreSQL 연결은 N과 무관하게 3개(세션 저장소, outbox, 알림 저장소가 각각 연결 하나를 락으로 보호)로 일정했다. 연결 고갈은 관찰되지 않았다. 연결 풀은 만들지 않았다: 병목이 연결이 아니기 때문이다. 별도 계측(N=50): 턴 한 번에 DB 문장이 약 29개(문장 하나는 단독일 때 약 0.5ms)이고, 부하 중 프로세스 CPU 시간이 벽시계와 거의 같았다(9.4초 CPU / 9.7초 벽시계). 즉 **한 프로세스가 CPU(파이썬 GIL) 한계에 닿아 처리량이 약 12~14 턴/초에서 포화**하고, 동시 수가 늘면 대기열처럼 지연이 늘었다(N=50 p50 2.8초 -> N=100 p50 5.3초). 연결을 늘려도 이 한계는 달라지지 않는다. 더 처리하려면 프로세스(복제)를 늘려야 하고 그러려면 PostgreSQL이 필요하다(SQLite는 단일 프로세스). 복제 구성은 측정하지 않았다.
- **스레드·메모리**: 요청마다 스레드 하나가 생겨(ThreadingHTTPServer) 동시 100 요청에서 서버 쪽 스레드가 약 200개까지 늘었다(위 스레드 수에서 클라이언트 100개를 뺀 값, 추정). 상한은 `SURVEY_API_MAX_INFLIGHT`(처리 단계), 그 앞의 접속은 제한이 없다. 메모리는 N=100에서 약 +100~160MB(스레드 스택 포함)였고 요청이 끝난 뒤 줄어드는지는 측정하지 않았다.
- **측정으로 찾아 고친 것**: `socketserver`의 기본 접속 대기열(`request_queue_size`)이 5라서 동시 접속이 몰리면 `ConnectionResetError`·세션 시작 실패(404)가 났다(N=50에서 19~38건). 128로 올렸고(`service._Server`), 그 뒤 같은 부하에서 연결 오류가 사라졌다.
- **N8 재시작 중 요청**: 코드를 읽은 결과 이전에는 SIGTERM이 `httpd.shutdown()`만 부르고 요청 스레드가 데몬이라 `server_close()`가 처리 중 요청을 기다리지 않았으며, 곧바로 `app.stop()`이 저장소를 닫아 처리 중이던 턴이 NOT_COMMITTED로 끝났다(시험으로 재현). 이제 SIGTERM은 (1) `draining`을 표시해 `/readyz`를 503 `shutting_down`으로 만들고(`/healthz`는 200), (2) 새 접속을 받지 않고, (3) 처리 중인 요청이 끝나기를 기다린 뒤(턴은 총 마감 8초 안에 끝난다) (4) 저장소를 닫는다. SSE 스트림은 0.5초 안에 끝낸다. docker의 기본 정지 유예(10초) 안에 들어온다. 한계: 접속 대기열에 있었지만 아직 accept되지 않은 연결은 소켓이 닫히며 버려진다. 재시작 순간 gateway가 어떻게 답하는지(502/503)와 키오스크의 재시도는 이 영역 밖이다. 시험: `tests/test_readyz_db_outage.py::test_graceful_shutdown_finishes_in_flight_turn_and_readyz_drains`. 실제 컨테이너의 `docker stop` 중 요청 시험은 하지 않았다(`scripts/smoke.sh`가 SIGTERM 정상 종료만 확인).

## 7. 소비자별 메모

| 이웃 | 메모 |
|---|---|
| release-gate | 활성 포인터는 값싸게 자주 읽힌다(기본 5초). 매니페스트 `content_hash`와 `question_set.content_hash`를 서비스가 다시 계산해 검증한다. `adapters.LORA_A/B.serving_config`가 달라지면 거부된다(model-runtime 0.3.0 전까지, CR-006). `turn_engine_version`·`model_runtime_version`은 이미지에 설치된 것과 MAJOR.MINOR가 같아야 한다. |
| ops-feedback | 봉투의 `release_id`는 세션이 묶인 릴리스다. `Id` 형식이 아닌 세션 ID·턴 ID는 `x-`+SHA-256 앞 32자로 바꿔 봉투에 넣는다(CONVENTIONS 10절). 원본은 `trace` 본문에 있다. 같은 (세션, 턴)이 두 번 올 수 있다(최소 1회). |
| 다른 영역의 문진 스키마 소비 | `openit-survey-schema`를 설치한다. 정수 버전을 여러 개 지원하면 `load_schema(name, version=N)`. |
