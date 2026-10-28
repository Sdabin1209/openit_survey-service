# survey-service 표준성·근거 전수 점검 (2026-10-29, D89)

기준: `head/docs/audit/standards-audit-spec.md`. 방식: 읽기 전용(코드·설정·시험 수정 없음, 비밀 값 읽지 않음, 시험 실행 안 함). 읽은 것: `CLAUDE.md`, `README.md` 앞부분, `docs/design.md` 일부(ADR 목록과 겹치는 부분 위주), `docs/adr/ADR-0001~0022` 전부, `survey_service/` 중 `settings.py`, `api.py`(인증·상수·_role), `service.py`(로깅·서버·스레드), `orchestrator.py`, `trace/outbox.py`, `trace/exporter.py`, `trace/pg_outbox.py`, `remote.py`, `pg.py`, `store/sqlite.py`(commit), `risk_rules.py`(앞부분·상수), `Dockerfile`, `requirements.txt/lock`. 읽기 허용된 `../boundary-contracts`에서 `canonical.py`, `PUBLIC_API.md:57`, `CONVENTIONS.md:118`만 읽었다.

분류: S 표준+근거, S- 표준이나 근거 기록 없음, N 비표준+근거, N- 비표준+근거 없음, O 재발명, ? 표준 여부 미확인. 표기: [확인] 파일에서 읽음, [판단] 내 해석.

## 1. 결과표

| ID | 요소 | 분류 | 표준 이름 | 근거 위치 | 증거 |
|---|---|---|---|---|---|
| A01 | 개발·시험 기본 저장소 SQLite(운영은 PostgreSQL) | N | 없음. ADR-0001이 인용한 "12-factor 개발/운영 동등성"은 [판단] 오인용: 12-factor 10항(dev/prod parity)은 개발과 운영의 백킹 서비스를 같게 두라는 것이고 SQLite↔PostgreSQL 차이를 경계한다 | ADR-0001(대안 비교 있음, D22) | settings.py:57-58, store/sqlite.py:50-55 |
| A02 | 운영 저장소 PostgreSQL, 서비스별 DB | S | 서버형 RDBMS·ACID·최소권한 계정 | ADR-0002, D85 | pg.py:54, ADR-0002 |
| A03 | psycopg3 직접, ORM·연결 풀 없음 | S | psycopg3 드라이버 관행. 풀 없음은 측정(가짜 모델 기준) | ADR-0003 | requirements.txt(psycopg 3.3.6), pg.py:72-95 |
| A04 | 세션 = 문서(JSON TEXT) 한 줄 + 정수 revision, 낙관적 검사 | S | 낙관적 동시성 제어(버전 열) | ADR-0006 | store/sqlite.py:21-26, 118-131 |
| A05 | 스키마 생성·이행을 직접 구현: 시작 때 `CREATE IF NOT EXISTS`, advisory lock, 컬럼 추가는 `_NEW_COLUMNS`/`ALTER`를 코드에 | O | 버전 있는 마이그레이션 도구(Alembic, Flyway 류)가 표준. [확인] `schema_version`/`user_version` 사용 없음(grep) | ADR-0002는 "멱등 생성"만 적고 도구를 쓰지 않는 이유 없음 | alerts.py:31,174,755; store/sqlite.py:59-61; pg.py:54,176-186 |
| A06 | SQLite WAL, synchronous=FULL | S | SQLite 문서의 WAL·synchronous 설정 | ADR-0001 | store/sqlite.py:53-54, trace/outbox.py:53-54 |
| B01 | HTTP 서버: 표준 라이브러리 ThreadingHTTPServer(요청당 스레드) | N | 표준은 프로덕션 WSGI/ASGI 서버. ADR-0004가 스스로 인정 | ADR-0004(대안 비교, 부하 측정). 단 "최소 의존" 논거는 의존 목록(langgraph, langchain-core, aiohttp 등, requirements.lock)과 맞지 않는다 [판단] | service.py:15,504-509 |
| B02 | HTTP 계층 방어를 직접 구현: 소켓 읽기 제한, 접속 상한 후 503, SSE 상한, Content-Length 검사, 본문 64KB | O | 프로덕션 서버·리버스 프록시(gateway)가 제공하는 기능 | ADR-0018은 gateway에만 맡기지 않은 이유(직접 열린 구성)를 적음. 서버 교체 대신 직접 구현한 이유는 ADR-0004(의존 최소)뿐 | service.py:371-377,504-523; api.py:51,178 |
| B03 | 방어 값 15초/256/50 | N- | 없음 | ADR-0018이 "근거 기록 없음, 측정 안 함"이라 적음 | settings.py:81-83 |
| B04 | 웹 푸시 endpoint SSRF 차단(https·443·공개 주소만, 리다이렉트 미추적) | S | OWASP SSRF Prevention Cheat Sheet의 방식(내가 이름을 붙임, ADR은 이름 없이 서술) | ADR-0018(DNS 재바인딩 한계 기록) | netguard.py:17-69 |
| B05 | 처리 중 요청 상한 32 | N | 없음. 근거를 ADR-0004/부하 측정(boundary.md §6-1)이 가리킴. 나는 boundary.md를 읽지 않아 측정 내용은 확인 못 함 | settings.py:109 주석 | settings.py:109 |
| C01 | LangGraph StateGraph로 턴 오케스트레이션(체크포인터·병렬 안 씀) | N | 없음(상태 기계의 그래프 표현은 일반 기법) | ADR-0005: 사용자 지시 D62. 대안은 손으로 쓴 if/else만 비교, 다른 엔진은 "근거 기록 없음". 외부 추적은 Dockerfile ENV로 끔(문진 발화 유출 방지) | graph.py(StateGraph), requirements.lock:29-32, Dockerfile:24-27 |
| D01 | 역할 토큰(kiosk, staff, result-reader, risk-source) Bearer, 상수 시간 비교 | S- | RFC 6750(Bearer). `hmac.compare_digest`는 Python 문서가 권하는 상수 시간 비교. [확인] 모든 토큰을 끝까지 비교(조기 반환 없음). 길이는 compare_digest가 드러낼 수 있으나 토큰 길이 노출은 실질 위험이 작다 [판단] | 역할 분리 자체의 ADR 없음(ADR-0016은 기기 토큰만) | api.py:111,222-233 |
| D02 | 기기별 키오스크 토큰 + 세션 소유 확인 | N | OWASP API Security Top 10(2023) API1 객체 수준 인가는 따름. 자격 방식(정적 Bearer 표)은 OAuth2 client credentials·mTLS 같은 표준 자격 발급이 아니다 | ADR-0016. 대안은 공유 토큰·추측 불가 ID만 비교, 표준 자격 방식은 비교 안 함 | service.py:339-340; api.py:236-250 |
| D03 | 토큰 발급·강도·회전: 길이 16자 최소는 기기 토큰에만, 역할 토큰은 "비어 있지 않고 서로 다름"만 검사, 회전은 재시작 | N- | 없음. 토큰 엔트로피 기준은 확인 못 함 | ADR-0016이 회전 불가를 감수로만 적음. 길이 16의 근거 없음 | api.py:105-118; service.py:340 |
| D04 | 직원 계정 ID를 `X-Openit-User` 헤더로 받아 신뢰(감사 기록·개입 기록용) | N- | 없음 | 주석에 "gateway가 인증"이라고만. 직접 열린 구성이나 직원 토큰 보유자가 임의 값을 넣을 수 있는 점의 근거·위험 기록 없음 | api.py:71,413; service.py:384 |
| D05 | 세션 ID는 클라이언트(프론트)가 정하고 서버는 형식(`[A-Za-z0-9_-]{1,64}`)만 검사. 서버 생성 아님 | N- | 서버 생성·CSPRNG를 권하는 OWASP Session Management 지침은 인증 세션 ID에 대한 것이라 그대로 적용되지 않는다 [판단]. 이 서비스는 세션 ID를 자격으로 쓰지 않고 토큰·기기 소유로 막는다 | ADR-0016 한 줄("생성 규칙은 프론트 소관")뿐. 왜 서버가 안 만드는지 기록 없음 | api.py:49,345; ADR-0016 |
| D06 | 요청 ID: 헤더 값(형식 검사) 또는 `uuid4().hex[:16]` | S- | UUID(RFC 9562)의 v4는 난수. 64비트로 줄임. 로그 상관용이라 충분 [판단] | 근거 기록 없음(감사 E1 주석) | service.py:41,357 |
| E01 | `turn_id` 멱등: (세션, turn_id) 키, 입력 해시 비교, 처리 중 409+Retry-After | S | IETF Idempotency-Key 초안(RFC 아님)의 패턴, RFC 9110 409 | ADR-0007 | orchestrator.py:116-123; store/sqlite.py:30-36 |
| E02 | 입력 해시 = `boundary_contracts.canonical.sha256_hash`(RFC 8785 JCS + SHA-256) | S- | RFC 8785. [확인] 구현은 boundary-contracts 소유의 자체 구현(`canonical.py`, 정수 ±2^53 제한)이고 이 리포는 가져다 쓴다 | 이 리포 ADR 없음(헤드 소관) | orchestrator.py:116; ../boundary-contracts/boundary_contracts/canonical.py:1-20 |
| E03 | 봉투 ID 변환: 형식 불일치면 `x-` + SHA-256 앞 32자(128비트) | S- | SHA-256(FIPS 180-4). 절단 길이의 근거는 없음 | 주석에 "결정적이라 멱등" | trace/exporter.py:30-36 |
| F01 | 서버 턴 마감 6초 + 바깥>안쪽 timeout 사슬(키오스크 10 > gateway 9.5 > 6) | S | 마감 전파(요청 전체 deadline), 바깥 timeout이 더 길어야 한다는 원칙. 값 6은 DB 장애 측정(pause 5.0초)과 gateway 상한에서 도출, **실제 LLM 지연은 측정 안 함**, 임시값(D88 U10) | ADR-0008 | settings.py:76; orchestrator.py:129-145 |
| F02 | DB 접속 2초 / 문장 2초 / 재접속 유예 1초 / readyz 총 2초 | S | 모든 외부 호출에 timeout, 빠른 실패. libpq connect_timeout 2초 미만은 2로 해석되는 제약. 측정(이 PC 한 곳) 있음 | ADR-0011 | settings.py:77-79,84; pg.py:34,53 |
| F03 | 추론 호출 timeout 4초, 재시도 1회, 선형 백오프 0.2초×회차(지터 없음), 타임아웃은 재시도 안 함 | N- | 지수 백오프+지터 권고(AWS Architecture Blog)와 다름 [판단]. 값 근거 없음 | 주석 "프론트 10초 안에 두 호출이 들어가게"는 서버 마감 6초(ADR-0008)로 바뀐 뒤 갱신 안 됨: 4초×2호출은 6초 초과 가능, 마감이 끊어 줌 [판단] | settings.py:115-117; remote.py:97-124 |
| F04 | 추출 입력 상한 2400자(이웃 상한 2500에서 여유 100) | N | 없음. 제약(이웃 서비스 400 기준)에서 나옴. 여유 100은 "임의" | ADR-0012(상태 "제안", 실제 문항 세트 칸 수 측정 안 함) | settings.py:80; ADR-0012 |
| F05 | 직원 호출 재알림 간격 30초 / 세션당 10회 | S | 429+Retry-After(RFC 6585, RFC 9110). 값은 계약 기본 권고(자리 표시값), **측정 안 함**을 ADR이 명시 | ADR-0021 | settings.py:118-119; alerts.py:120-121 |
| F06 | 직원 호출 보고 상한 20 (재알림 상한의 2배), 세션 잠금 줄무늬 64개 | N | 없음. 20은 규칙(2배)으로 도출, 측정 안 함. 64는 이유 기록 없음 | ADR-0021(20만). 64의 근거 없음 | settings.py:120; alerts.py:122; ADR-0021 |
| F07 | 연속 되묻기 상한 3, 4번째에 직원 호출 | N | 대화 시스템 fallback/escalation 관행(이름 있는 표준 아님). 학습 데이터가 단일 턴이라 **측정 못 함**. ADR이 "FAULT_POLICY의 재시도 3회 관행과 맞춤"이라 적었으나 나는 FAULT_POLICY를 읽지 않았다 | ADR-0020(ADR-0014 대체) | settings.py:121; graph.py `_reask_escalation` |
| F08 | 키오스크 재시도 총 예산 약 20초(지수 백오프+지터) | ? | 이 리포가 구현하는 값이 아니다. 계약이 "잠정"이라 적음. 표준 이름 없음. 서버 쪽 대응(F01의 6초, F05의 30초)과 어떻게 맞는지 이 리포 문서에 기록 없음 | boundary-contracts PUBLIC_API.md:57, CONVENTIONS.md:118 | ../boundary-contracts/PUBLIC_API.md:57 |
| F09 | 값 검증 계층(형식→의미→값 타당성), 모델 출력 불신 | S | 심층 방어/입력 검증(OWASP Input Validation). 학습 데이터 32,170행으로 시험 | ADR-0017 | ADR-0017 |
| F10 | 값 타당성 상한: 숫자 100만 이하, 문자열 200자 | N- | 없음 | ADR-0017이 "근거 기록 없음(거친 상한)" | ADR-0017 |
| F11 | 그 밖의 상수 묶음(근거 없음): 본문 64KB, 유휴 만료 300초, 상태 폴링 0.5초, Retry-After 1초, 푸시 구독 100, 릴리스 폴링 5초, 시작 대기 5초, 릴리스 재시도 5/60초, HTTP 클라이언트 10초, 릴리스 보관 2, 푸시 timeout 3초, 푸시 TTL 3600초, 알림 이벤트 보존 1만, SENT 보관 24시간, 발화 1000자 | N- | 푸시 TTL은 RFC 8030의 TTL 헤더 값. 값의 근거는 아님. 발화 1000자는 ADR-0012가 "계약 값"이라 적음(계약 쪽 근거는 안 읽음) | 주석/ADR 없음(일부 주석은 무엇인지만 설명) | api.py:51; settings.py:85-87,95,102,104-106,111,122,126; alerts.py:282,518; service.py:179 |
| G01 | 보존 정책: 기본값 유한, 무기한(null·0) 시작 거부, 설정으로 변경 | S- | 개인정보 보호 기본 설정(GDPR 25조 등)과 보존 최소화. ADR은 이름 없이 "privacy by default"만 적음 | ADR-0015 | settings.py:27-38 |
| G02 | 보존 값: 세션 90일, 접근 기록 365일, 해결 알림 365일, DEAD outbox 30일 | ? | 값의 출처는 사용자 지시(D88 U4, 잠정)이고 측정·표준 근거 아님. 진료·개인정보 관련 법정 보존 기간이 이 데이터에 적용되는지 **확인 못 함**(ADR은 "법정이 더 길면 늘림"만 적음) | ADR-0015 | settings.py:69-72 |
| G03 | 보존 정리를 서비스 안 스레드(3600초 주기)와 CLI `purge` 둘 다로 실행 | N- | D39는 잡 실행을 deploy 스케줄러에 두라 함 [확인: 워크스페이스 CLAUDE.md]. 서비스 안 스레드를 병행하는 이유 기록 없음. 주기 3600초 근거 없음 | ADR-0015는 둘 다 있음만 적음 | service.py:193-207; settings.py:73 |
| H01 | Trace 영속 outbox: 턴은 로컬 INSERT만, 전송 스레드가 재시도, 수신 쪽 (세션, 턴) 멱등, UNAVAILABLE만 재시도, 나머지는 DEAD 격리 | S | outbox 패턴(영속 버퍼+비동기 릴레이), 최소 1회 전달+수신 멱등 | ADR-0009 | trace/outbox.py:17-33,73-90; trace/exporter.py:107-141 |
| H02 | "트랜잭션 outbox" 주장 | N- | 트랜잭셔널 아웃박스(microservices.io, Richardson)는 업무 데이터와 **같은 DB 트랜잭션**에 행을 쓰는 것이 요건. [확인] 이 구현은 세션 commit이 끝난 뒤 별도 호출로 넣고(`save_turn_result` 뒤 `_submit_trace`), 실패는 삼키며(`trace_enqueue_failed` 센 뒤 계속), SQLite에서는 파일이 다르고 PostgreSQL에서도 연결이 다르다. commit과 enqueue 사이 크래시에서 Trace가 유실될 수 있다 [판단]. ADR-0009는 이를 "트랜잭션 outbox 패턴"이라 부르고 "유실 없는 재전송"을 얻는 것으로 적었으나 이 유실 창은 기록하지 않았다(상한 초과 유실만 기록) | 유실 창에 대한 근거·수용 기록 없음 | orchestrator.py:165-167,260-267; store/sqlite.py:118-131; trace/pg_outbox.py:12-14 |
| H03 | outbox PENDING 상한 20000 | N | 없음. 크기 추정(실측 안 함)에서 도출 | ADR-0009가 추정이라 명시 | settings.py:125 |
| H04 | 전송 재시도: 지수 백오프 1~60초, 지터 없음, 단일 전송자 | S- | 지수 백오프(+지터 권고)는 일반 관행. 단일 전송자라 지터 영향이 작다 [판단] | ADR-0009에 값 근거 없음("1~60초") | trace/exporter.py:119-123; settings.py:123-124 |
| I01 | 직원 알림 스트림 SSE, 순번 `id:`+`Last-Event-ID` 재개 | S | WHATWG HTML Living Standard의 server-sent events | ADR-0010(WebSocket 비교) | service.py:440-480 |
| I02 | SSE 하트비트 15초 | S- | WHATWG HTML 표준이 프록시 연결 유지를 위해 약 15초마다 주석 줄을 권한다고 기억하나 이 세션에서 원문을 다시 확인하지는 못했다 | ADR-0010은 하트비트 이름만 적고 15초 근거 없음 | settings.py:103; service.py:476 |
| I03 | 웹 푸시: pywebpush + py-vapid, VAPID 키는 비밀 파일/환경, subject mailto:/https: | S- | RFC 8292(VAPID), RFC 8030(Web Push), RFC 8291(메시지 암호화). 직접 구현이 아니라 라이브러리 사용 | ADR-0010 "병행한다(D64)"뿐. 푸시 선택·키 보관 ADR 없음 | alerts.py:516-545; service.py:87-92; requirements.lock:39,45 |
| J01 | 위험 감지: 모든 발화에 결정적 규칙을 모델·DB와 독립으로 선적용, 민감도 우선 | N | 의료 triage fail-safe 원칙을 ADR이 이름 없이 인용. 한국어 임상 위험 발화 표준은 **확인 못 함** | ADR-0013(D88 U1·U2, 대안 비교) | graph.py `prescreen`; risk_rules.py:1-20 |
| J02 | 부정·제3자·과거·숫자 문맥 휴리스틱과 그 상수(`_NEG_GAP_MAX=8` 등), 직접 쓴 한국어 부정 처리 | N- | 임상 텍스트 부정 탐지의 표준 알고리즘(NegEx 등)이 있으나 한국어 적용 여부는 확인 못 함. 상수 도출 기록 없음. ADR-0013이 보인 측정은 적중 수 감소(20→4)이고 미탐(놓친 위험)은 측정 안 함 | ADR-0013 2차 보완(오탐 위주) | risk_rules.py:110-145 |
| J03 | 임상 검토 전 임시 기본 목록을 기본 켬 | ? | 임상 검토 결과가 없어 적정성 확인 불가. `provisional=true`를 /readyz에 드러냄 | ADR-0013 | risk_rules.py:148; ADR-0013 |
| K01 | 로그: JSON 한 줄, 표준 출력, request_id | S- | 12-factor 11항(로그는 이벤트 스트림) | ADR 없음(워크스페이스 규칙, 주석) | service.py:44-60 |
| K02 | 민감정보: 발화·모델 출력·Trace를 로그에 내지 않음, 알림 증거는 규칙 ID만, 비밀은 출처(file/env)만 /readyz에 | S- | 최소 로깅(OWASP Logging Cheat Sheet). 로그 호출 전수 검사는 하지 못함 | 주석(service.py:45, risk_rules.py 머리말) | service.py:45,229-240; risk_rules.py:12-14 |
| K03 | 지표: 프로세스 안 카운터를 /readyz JSON으로 노출 | ? | `boundary-contracts/METRICS.md`가 있으나 읽지 않아 표준 대응 여부 확인 못 함 | 근거 기록 없음 | orchestrator.py:267-275 |
| L01 | 비밀은 `NAME_FILE`로 받고 `read_secret` 사용, 환경 변수는 호환 | S | 12-factor 3항 + Docker/Kubernetes secret 파일 관행(D84) | D84 | service.py:70-75 |
| L02 | 이미지·의존 고정: python:3.12-slim(태그만, digest 없음), 비root, 라벨, `requirements.lock` 정확 버전 | S- | Docker 모범 사례(비root 등). 기반 이미지 digest 고정과 pip `--hash` 검증은 없음 | 워크스페이스 D22, 이 리포 ADR 없음 | Dockerfile:3,10,22-23; requirements.lock(해시 줄 없음) |
| L03 | 시간: 마감·간격은 `time.monotonic`, 저장 시각은 `time.time`(시계 주입 가능) | S- | Python 문서(간격에는 monotonic) | 주석 일부, ADR 없음 | orchestrator.py:129; graph.py:151-155; alerts.py:161 |
| M01 | 동시성 모델: 요청당 스레드, 세션 락(프로세스 안), 턴은 데몬 스레드에서 돌고 마감 시 취소 표시만 함(진행 중 호출은 못 끊음) | N | 없음. 프로세스 복제 시 DB 행 잠금·revision 검사에 의존 | ADR-0004·0007(한계 명시) | orchestrator.py:129-150; ADR-0007 |

## 2. 집계 (52개 요소)

| 분류 | 개수 | ID |
|---|---|---|
| S | 13 | A02, A03, A04, A06, B04, E01, F01, F02, F05, F09, H01, I01, L01 |
| S- | 12 | D01, D06, E02, E03, G01, H04, I02, I03, K01, K02, L02, L03 |
| N | 11 | A01, B01, B05, C01, D02, F04, F06, F07, H03, J01, M01 |
| N- | 10 | B03, D03, D04, D05, F03, F10, F11, G03, H02, J02 |
| O | 2 | A05, B02 |
| ? | 4 | F08, G02, J03, K03 |

## 3. N-, O, ? 목록

- A05 (O): 스키마 생성·이행(컬럼 추가)을 코드에 직접 구현, 버전 있는 마이그레이션 도구를 쓰지 않는 이유 기록 없음.
- B02 (O): HTTP 방어(읽기 제한, 접속 상한, 본문 제한)를 직접 구현. 서버를 바꾸지 않은 근거는 ADR-0004의 "최소 의존"뿐이고 그 논거는 실제 의존(langgraph 계열, aiohttp)과 맞지 않는다.
- B03 (N-): 소켓 15초, 접속 256, SSE 50은 근거 없음(ADR-0018이 명시).
- D03 (N-): 역할 토큰의 강도 기준·발급·회전 절차에 기록 없음(16자는 기기 토큰만).
- D04 (N-): `X-Openit-User` 헤더 신뢰의 위험·근거 기록 없음.
- D05 (N-): 세션 ID를 클라이언트가 정하고 서버가 만들지 않는 이유 기록 없음.
- F03 (N-): 추론 timeout 4초, 재시도 1, 백오프 0.2초(지터 없음)의 근거 없음, 주석의 논리가 6초 마감 도입 뒤 갱신 안 됨.
- F10 (N-): 값 타당성 상한 100만, 200자 근거 없음(ADR-0017이 명시).
- F11 (N-): 상수 묶음(본문 64KB, 유휴 300초, 상태 폴링 0.5초, 푸시 구독 100, 릴리스 폴링 5초, 푸시 TTL 3600초, 이벤트 보존 1만, SENT 24시간 등) 근거 없음.
- G03 (N-): 보존 정리를 서비스 스레드와 CLI로 이중 운영, D39(deploy 스케줄러)와의 관계·3600초 근거 없음.
- H02 (N-): "트랜잭션 outbox"라 부르나 세션 commit과 같은 트랜잭션이 아니다. commit 뒤 별도 enqueue, 실패는 삼킴, commit과 enqueue 사이 크래시에서 Trace 유실 가능. 이 유실 창은 ADR-0009에 없다.
- J02 (N-): 한국어 부정·문맥 휴리스틱과 상수의 도출 근거 없음, 미탐은 측정 안 함.
- F08 (?): 키오스크 재시도 예산 약 20초는 계약(PUBLIC_API.md:57, CONVENTIONS.md:118)의 잠정값이고 이 리포가 구현하지 않는다. 표준·측정 근거는 확인 못 함.
- G02 (?): 90/365/365/30일의 법정·규정 근거 확인 못 함(사용자 지시 잠정값).
- J03 (?): 임상 검토 전 임시 위험 목록의 적정성 확인 못 함.
- K03 (?): /readyz 카운터가 METRICS.md 규약에 맞는지 확인 못 함(METRICS.md 미독).

참고: A01은 N이지만 ADR-0001의 표준 인용(12-factor)이 반대 방향이므로 ADR 본문 정정이 필요하다.

## 4. 점검하지 못한 것

- 시험·이미지 빌드·서버를 실행하지 않았다. 모든 판단은 읽기만으로 했다.
- `docs/design.md`는 앞부분과 ADR 목록 중심으로만 훑었고 전체를 읽지 않았다. `boundary.md`(부하·장애 측정 §5·§6-1), `CHANGELOG.md`, `runbook.md`, `STATUS.md`는 읽지 않아 ADR이 인용한 측정은 재확인하지 못했다.
- 코드 중 `graph.py`(894줄), `alerts.py`(770줄), `api.py`, `service.py`, `response_tables.py`, `extraction.py`, `release/loader.py`, `store/memory.py`, `store/effects.py`, `scripts/`, `tests/`는 grep과 부분 읽기만 했다. 로그 호출 전수 검사(발화·식별자 유출)와 알림 DB 스키마 전부는 하지 않았다.
- `FAULT_POLICY`, `METRICS.md`, `HTTP.md`, 이웃 영역(키오스크, gateway, deploy)은 읽지 않았다. 따라서 timeout 사슬의 바깥 값(10초, 9.5초)과 G02의 법정 기간은 확인하지 못했다.
- 이 세션에서는 외부 표준 원문을 열람하지 않았다. 표준 이름과 요구 내용은 내 기억에 근거하며, 특히 I02(WHATWG 15초), A01(12-factor 10항), H02(트랜잭셔널 아웃박스 요건)는 원문 대조가 필요하다.
- `requirements.lock` 전체 의존의 라이선스·취약점은 보지 않았다.
