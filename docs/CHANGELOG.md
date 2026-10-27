# 변경 이력

## 설치 줄을 태그 기준으로 고침 [미출시, 푸시·태그 전]

- 증상(새 venv, 이 PC에서 재현): README/runbook의 `pip install -e ../boundary-contracts ../turn-engine ../model-runtime -e schema -e . ...`가 `ResolutionImpossible`. 원인: turn-engine이 boundary-contracts를 Git 태그 v2.0.2로 요구하는데 같은 줄에서 로컬 경로로도 요구했다. 고침: `pip install -c requirements.lock -r requirements.txt -e .`. 이웃 라이브러리는 원격 태그(워크스페이스 규칙, 이미지·build_wheels와 같은 방식, 옆 폴더 비의존)에서 설치한다. `requirements.txt`의 `./schema`를 `-e ./schema`로(시험이 리포의 schema/ 경로를 확인), `setuptools`·`wheel`을 추가(시험이 `--no-build-isolation`으로 wheel을 빌드), 잠금 파일을 제약으로 쓴다(없으면 langgraph가 1.2.18로 풀려 잠금 값을 확인하는 시험이 실패).

## 의존 태그 올림: model-runtime v0.7.1 -> v0.7.2 [미출시, 푸시·태그 전]

- model-runtime 의존을 v0.7.2로(requirements.txt, build_wheels 기본 태그, inference.py 주석, boundary·STATUS의 현재 기준 표기). v0.7.2는 가짜 A에 정정 출력을 추가한 하위 호환 변경이다. `requirements.lock`에는 model-runtime 줄이 없어(서드파티만 고정, 이웃은 wheel) 바뀌지 않는다. `scripts/build_wheels.sh`는 이웃 폴더의 `git archive` 대신 원격 태그를 얕게 clone해 wheel을 만든다(이웃 폴더를 읽지 않는다).

## /readyz가 PostgreSQL 장애 때 상한 안에 503으로 답한다 (CR-D23) [미출시, 푸시·태그 전]

- 원인(이 PC에서 측정, 로컬 PostgreSQL 컨테이너): `/readyz`가 세 저장소의 `ping`을 **직렬로, 요청 경로와 같은 연결·락으로** 불렀다. 연결 불가(응답 없는 주소) 시 한 확인이 `connect_timeout=10`초를 그대로 기다려 **세 확인 30.0초**(10.0+10.0+10.0, 두 번 반복해도 같음)였다. outbox 전송 스레드가 같은 락을 쥐고 접속을 시도하면 거기에 더 기다린다(관찰한 37초와 같은 자릿수, 추정). 컨테이너를 `docker pause`(연결은 서버가 붙잡고 응답 없음)하면 주 연결 질의에 상한이 없어 120초 넘게 응답하지 않았다. 또 DB를 읽는 보조 수치(`exporter.stats`, `alerts.status`)의 예외가 처리되지 않아 핸들러가 죽고 연결이 끊겼다(RemoteDisconnected의 직접 원인으로 보이나 컨테이너 환경에서는 재측정 전). `docker stop`(연결 거부)만은 이전에도 0~4초였다.
- 고침: PostgreSQL 확인은 **별도 짧은 연결**(`PgConn.probe`, 주 연결·락 불사용, connect_timeout과 서버 statement_timeout = `SURVEY_READYZ_DB_TIMEOUT_SECONDS` 기본 2초)로 세 저장소 동시에, 전체 +1초 하드 상한. 보조 수치는 DB 정상일 때만 1초 한도로 읽고 아니면 마지막 값에 `db_stats_stale: true`. 503 본문에 `database.status=UNAVAILABLE`, `detail="db unavailable"`(접속 정보 없음). 바꾸지 않은 것: 턴 처리 경로, 재접속·재시도 규칙, 연결 timeout 10초, SQLite 동작, 기존 `problems` 이름.
- 시험: `tests/test_readyz_db_outage.py` 4개(연결 불가 시 상한 안 503과 복구 후 200, 주 연결 락을 쥐고 있어도 막히지 않음, 확인이 멈춰도 하드 상한, 설정 값).
- 못 한 것: 실제 compose 환경 재측정(deploy E2E 5p에서 헤드). 응답 없는 DB에서 요청 경로가 최대 10초 기다리는 점은 그대로(바꾸면 환자 경로 의미가 바뀌어 제안만: 요청 경로의 연결 timeout 단축은 별도 결정).

## B 입력의 표 찾기를 데이터셋 담당의 원본 규칙으로 교체 [미출시, 푸시·태그 전]

- 이전 `response_tables.json`(`DRAFT_UNCONFIRMED`)은 학습 샘플에서 역으로 읽은 초안이었고 원본을 모르고 만든 것이었다. 원본은 학습 서버 `~/WS/dataset/scripts/labels.py`(2026-10-14, 데이터셋 담당; README가 '서비스 표 찾기의 기준 구현'이라 부른다)다. 이 규칙을 옮겼다: 어휘와 고정 문장은 `schema/data/response_tables.json`(`ResponseTables` 스키마를 새 구조로), 조합 로직(그룹핑, 조사, 시간·년 표기, 술 단위, previous/current)은 `survey_service/response_tables.py`의 `b_fields`. 서버 파일 내용은 리포에 복사하지 않았고 출처만 적었다. 이유(기준 선택): 학습 데이터가 이 규칙으로 만들어졌으므로 서비스가 이 규칙을 따라야 모델이 학습한 입력 분포를 받는다.
- 상태: `table_status = DATASET_RULES_UNCONFIRMED`(데이터셋 담당 확인 대기). 표 승인 기록은 찾지 못했다(IO_SPEC 7장 2번은 미정). 원본에 없는 서비스 쪽 판단은 57행 문제의 남은 칸 문장(`remaining`)뿐이고 학습 분포 밖이라는 점을 파일 `note`와 design 18절에 그대로 적었다.
- 동작이 바뀐 곳(원본을 따르므로): `topic`은 대상 칸(`targets[0]`)이 있으면 그 문항(이전: 첫 값의 문항), `known`은 CONFIRM만 빈 문자열이고 나머지 action은 ANSWERED·CORRECTION 값 전부(이전: NEXT·CORRECT·REASK의 `save=true` 값만), 조사는 원본 규칙(숫자 끝소리 대략). `extraction.build_response_request`는 문장 필드를 `ResponseTables.b_fields`에서 받고 `next`만 붙인다. 이전의 `_confirm_texts`와 표 규칙 매칭(`fnmatch`)은 없앴다.
- 폴백: 이전에는 표에 규칙이 없는 (사유, 칸) 199개 조합이 폴백이었다. 원본의 일반 분기(`else`)가 만드는 문장은 원본의 문장이므로 폴백이 아니다(`counters.ask_generic`). `ask_fallback`·`phrase_fallback`은 원본 규칙이 예외를 던질 입력(표에 없는 문항, 표에 없는 Q3 값)에서만 늘고 지어내지 않는다. `/readyz`의 `response_tables`에 `counters`가 나온다.
- 확인(서버 데이터는 표준출력으로 흘려 이 PC에서만 계산, 저장하지 않음, 서버에는 `cat`과 `ls`만): 학습 데이터 train·val·test의 START를 뺀 B 입력 5,077/5,077행(train 3,982, val 549, test 546)에서 `next`를 뺀 필드 글자 단위 일치, START 110/110, 폴백 카운터 0. 원본 코드와의 조합 비교: ask 20,332 조합, 다중 대상 136, value_text 930 + 표 밖 값 204, 무작위 b_input 40,000 전부 같음. 시험으로 남긴 것: 서버 데이터 없이 도는 대표 쌍(`tests/data/response_tables_golden.json`, 규칙 함수의 출력, 출력 문장마다 하나씩 ask 191 + value 128 + b_input 160)과 `tests/test_response_tables_rules.py`. `scripts/table_agreement.py`는 새 방식(전수 대조)으로 다시 썼다.
- `/readyz`의 `response_tables.status`와 smoke 검사를 `DATASET_RULES_UNCONFIRMED`로 바꿨다.

## D85 2단계: 저장소를 PostgreSQL로(세션, outbox, 알림) [미출시, 푸시·태그 전]

- 설정 `SURVEY_DATABASE_URL`(`read_secret`, `_FILE` 우선). 있으면 PostgreSQL, 없으면 기존 SQLite(기본 그대로). 세 저장소(`PgSessionStore`, `PgOutbox`, `PgAlertStore`)는 SQLite 구현을 상속해 **같은 SQL 본문**을 쓰고 연결·스키마 방언만 다르다(`survey_service/pg.py`, `store/postgres.py`, `trace/pg_outbox.py`, `alerts.py`의 `PgAlertStore`, 선택은 `db.py`). 이유: SQL을 두 벌 두면 어긋나므로 한 벌을 두 방언에서 같은 시험으로 검증한다. 이를 위해 SQLite 쪽도 `INSERT OR IGNORE`를 `ON CONFLICT DO NOTHING`으로, `lastrowid`를 `RETURNING`으로 바꿨다(SQLite 3.35+).
- 이유(드라이버): `psycopg[binary]==3.3.6`을 requirements·lock·pyproject에 고정(ops-feedback, release-gate와 같은 방식, libpq 내장 wheel이라 이미지에 시스템 패키지가 필요 없다). JSON은 TEXT로 저장(SQLite와 바이트까지 같다).
- 끊긴 연결: `PgConn`이 연결이 죽어 있으면 트랜잭션 시작 전에 다시 열고, 트랜잭션 밖 멱등 한 문장은 새 연결로 한 번 다시 시도한다. **트랜잭션 도중에 끊기면 다시 시도하지 않는다**(서버가 되돌렸고 COMMIT 중이면 반영 여부를 몰라 UNKNOWN). 이유: 세션 쓰기는 낙관적 revision 검사가 있어 몰래 재시도하면 반영된 쓰기를 충돌로 오판한다. 세션 쓰기에는 행 잠금(`FOR UPDATE`)을 건다.
- 접속 문자열 비노출: 오류는 예외 클래스 이름만(`세션 DB 접속 실패: OperationalError`), 재접속 실패도 드라이버 메시지(호스트·사용자 포함)를 쓰지 않고 자체 문구. 시험이 암호 카나리 문자열로 오류·로그·`/readyz`를 확인한다. 시작 실패 로그에 `StoreError` 사유(비밀 없음)를 낸다.
- `/readyz`에 `database: {kind: postgresql|sqlite}`. 스키마는 시작 때 멱등 생성(advisory lock).
- 시험: 세션 저장소 적합성(메모리·SQLite·PostgreSQL 같은 시험), 알림 DB·outbox 적합성(SQLite·PostgreSQL), D85 1단계 시험을 두 백엔드로 돌림, 끊은 연결 재접속(세션·알림·outbox·앱 전체), 트랜잭션 중 끊김, 접속 문자열 비노출. `TEST_DATABASE_URL`이 없으면 PostgreSQL 시험은 SKIP(통과로 세지 않는다). `SMOKE_PG=1 scripts/smoke.sh`가 같은 이미지를 PostgreSQL로 띄워 재시작 뒤 세션 유지와 비노출을 확인한다.
- 하지 않은 것: SQLite 데이터를 PostgreSQL로 옮기는 도구, 세션 저장소와 알림 DB를 한 트랜잭션으로 묶기(여전히 별개 트랜잭션), TLS(deploy 문서: 내부 네트워크 한정), 커넥션 풀(저장소마다 연결 하나를 락으로 보호하는 기존 구조 유지).

## D85 1단계: subject_ref, 결과 조회, 외부 위험 신호, 직원 식별, 세션 보존 [미출시, 푸시·태그 전]

정의는 boundary-contracts `PUBLIC_API.md` 추가 인터페이스 A~F절. 비어 있는 부분은 표준적인 방법을 골랐다(이유 한 줄씩).

- **A. `subject_ref`**: 시작 본문 선택 필드, `[A-Za-z0-9._:/#=+-]` 1~128자 검증, 알림 DB의 `session_meta`에 저장(세션 문서 스키마 불변). `SURVEY_REQUIRE_SUBJECT_REF` 기본 꺼짐. 로그·알림·푸시·Trace에 넣지 않는다(오케스트레이터에 넘기기 전에 떼어 낸다). 이유: 환자 식별 값은 업무 데이터와 분리해 한 곳에 두고 보존 정리 때 함께 지우기 위해. 문진 스키마 `FrontendMessage.json`의 SESSION_START에 선택 필드 `subject_ref`를 정식으로 추가했다: **정수 CONTRACT_VERSION은 올리지 않는다. 선택 필드 추가라 기존 메시지가 그대로 통과하고(호환 추가), 다른 kind에는 허용하지 않는다.** 시험 `test_frontend_message_schema_accepts_subject_ref_only_on_session_start`.
- **C. `StaffSessionView`**: 최상위 `subject_ref`, `alerts[].resolved_by`를 더했다(둘 다 필수·null 허용). CR-013은 PUBLIC_API.md C절에 반영됨(상단 표시).
- **B. 결과 조회** `GET /v1/results/{session_id}`, `GET /v1/results?subject_ref=&status=&updated_after=&limit=&cursor=`(역할 result-reader, 스키마 `SessionResult`). 부분 결과도 상태와 함께, 발화·대화 로그 없음, 호출마다 접근 기록(목록은 돌려주는 세션마다 한 행, 조회 조건은 이름만 `detail`에 남기고 환자 참조 값은 남기지 않는다), 못 남기면 503. 목록 순서는 (updated_at, session_id)이고 cursor는 그 위치다. 이유: 병원 시스템이 `updated_after`로 증분 조회하는 방식이 표준이고 세션 갱신 시각 순서가 그에 맞아서. `/readyz`의 `result_dispatcher`는 NO_TARGET 그대로.
- **D. 위험 신호** `POST /v1/sessions/{id}/risk-signals`(역할 risk-source, 스키마 `RiskSignal`): `signal_id`로 멱등(재전송 200, 같은 `alert_id`), 세션 없음 404, 끝남 409. 효과는 알림(`rule_id=source:code`)+`PAUSED_RISK`(정지 기록에 신호 출처·코드)이고 직원 콘솔(SSE/푸시)로 전달된다. 내부 규칙 엔진은 같은 표기(`signal:internal:<id>`, rule_id `internal:<id>`)를 쓰는 기본 감지기로 정리했다. 이유(턴 밖 커밋): 신호는 발화 턴과 무관하게 오므로 대화 로그 항목을 만들지 않고 진행 기록의 정지 이력에만 남긴다(세션 문서 스키마를 바꾸지 않으려고). 알림 저장과 신호 기록은 한 트랜잭션이 아니다: 그 사이에 죽으면 같은 `signal_id` 재전송이 알림을 한 번 더 만들 수 있다. 앞선 한계(규칙을 검증 앞에서 평가하는 문제)는 이 API로 대체되어 다루지 않는다.
- **E. 직원 식별**: 개입에서 `X-Openit-User`를 `staff_id`로 대화 로그 개입 항목(`LogEntry.staff_id`, SessionStore·TurnTrace·OrchestratorOutput의 선택 필드 추가, 정수 버전 불변: 호환 추가)과 알림 `resolved_by`에 남긴다. 헤더 없음 401, 형식 오류 400, 본문의 `staff_id`는 무시. 같은 turn_id 재전송은 처음 기록을 지킨다(멱등).
- **세션 보존**: `SURVEY_SESSION_RETENTION_DAYS`(기본 없음=삭제 안 함), run-once 잡 `python -m survey_service purge`. 끝난 세션의 세션 문서·턴 기록·환자 참조·시각·신호 기록을 지운다. 접근 기록·알림 행은 남긴다. 알림 DB 쪽을 먼저 지워 중간에 죽어도 다시 돌리면 이어진다(멱등). 이유: 스케줄러가 언제 돌릴지 정하고 서비스는 스스로 깨우지 않는다는 워크스페이스 규칙(D39).
- 시험: `tests/test_results_signals_retention.py`(18개). 기존 시험은 개입 호출에 `X-Openit-User`를 붙이도록 고쳤다.

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
