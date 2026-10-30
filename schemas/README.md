# schemas/ (영역 내부 타입)

JSON Schema draft 2020-12, 파일 하나 = 타입 하나, 파일 이름 = 타입 이름(DESIGN.md 3절). 통과·거부 예시는 `examples/<타입>.valid*.json`, `<타입>.invalid*.json`.
영역 사이 타입(boundary-contracts)과 문진 스키마(`../schema/`)는 복사하지 않고 `x-contract`, `x-survey-schema` 주석으로 가리키며 검증기(`survey_service/types/validation.py`)가 그 정본으로 위임한다.
점검: `.venv/bin/python scripts/check_schemas.py`. 코드 타입 생성: README 4절. 컴포넌트와 타입의 대응: [docs/components.md](../docs/components.md).

작성 규칙: 코드의 거부 지점(if 검사, raise, ValueError, ConfigError, 4xx 응답)을 훑어 스키마로 옮겼다. 모든 거부 지점을 옮긴 것은 아니다. 옮기지 못했거나 오늘 옮기지 않은 것은 아래 표에 있다.

## 스키마 밖 조건

현재 64건(목표 0건에는 못 미쳤다). 대부분 두 문서 사이의 관계(입력과 출력의 변환 관계, 두 입력의 소속)나 저장소·시간 같은 상태가 필요한 조건이라 단일 문서의 JSON Schema로 쓸 수 없다. 이 부류의 조건은 컴포넌트 시험과 호출하는 wiring이 보증한다(표의 마지막 칸). 주의: 이 표에 있는 코드 검사 중 일부는 컴포넌트 안에 아직 손 검사로 남아 있다(스키마 검증으로 바꾸지 못했다). 그 목록과 이유는 표의 각 행이다.

한계 하나: `types/validation.py`가 `x-contract`·`x-survey-schema`를 따라가는 자리는 properties, items, allOf, 구조가 맞는 anyOf·oneOf 가지, 성립한 if의 then, 같은 파일 안 $ref다. 그 밖의 자리에 단 주석은 문서 역할만 한다.

| 컴포넌트 | 조건(코드 위치) | 스키마로 못 쓰는 이유 | 어디서 보증하는가 |
|---|---|---|---|
| turn_route | 입력 종류 x 세션 상태 -> 칸 대응(`COLUMNS`, `_DEFAULT`, `route`) | TurnRouteQuery와 TurnRoute 두 문서 사이의 함수 관계다. 한 문서의 스키마는 다른 문서의 값을 참조하지 못한다. 출력끼리의 일관성(turn_kind null이면 create·full false 등)만 TurnRoute에 적었다 | 처리 표 시험(tests/test_graph.py, tests/test_orchestrator.py) |
| reask_cap | 출력 = 서비스 상한과 문항 세트 상한 세 개 중 가장 작은 값(`effective_reask_cap`) | 입력과 출력 사이의 계산(min)이다 | tests/test_reask_limit.py, tests/test_readyz_reask_effective.py |
| flow_table | skips[].skip_item이 order 안에 있다(`build_flow`의 ValueError) | 배열 한 곳의 값이 같은 문서의 다른 배열에 있는가는 JSON Schema로 쓰지 못한다(uniqueItems는 QuestionFlow가 이미 막는다) | 코드의 ValueError와 문진 스키마 적재 시험. 이 조건은 문진 스키마(QuestionFlow) 소유 영역인 survey-service가 책임진다 |
| flow_table | FlowView.table이 FlowSource의 order·skips와 같다, reference_sources·slot_order·flow_id를 그대로 옮긴다(`build_flow`) | 입력과 출력 사이의 복사 관계다 | tests/test_extraction.py |
| question_bank_view | BankView.questions가 order 오름차순이다(`ordered_questions`) | 배열 정렬 여부는 JSON Schema로 쓰지 못한다 | tests/test_extraction.py 등 문항 세트 시험 |
| question_bank_view | 질문 문장·고정 문장 찾기(`Bank.sentence`, `fixed_sentence`) | 조회 질의(키 -> 값)이고 문서 하나의 형태 조건이 아니다 | 코드, 호출하는 wiring의 시험 |
| field_specs | choice_catalog의 code 중복, 문진 스키마와 계약 사이의 어긋남(`invalid_field_specs`) | 이 검사는 거부가 아니라 FieldSpecIssues를 내는 업무다. 입력 문서가 아니라 입력에서 만든 규격과 계약을 대조한다 | tests/test_question_fields.py, tests/test_bank_load_rules_320.py. 계약 모양은 FieldSpecs의 x-contract가 검증기에 위임 |
| field_specs | 자료형(data_type)이 적힌 칸이 하나도 없으면 출력이 없다(`build_field_specs`가 None) | 출력이 없다는 사실은 출력 문서의 스키마가 아니다. FieldSpecs는 있는 경우만 적는다 | tests/test_question_fields.py. 호출하는 wiring이 None을 보고 규격을 싣지 않는다 |
| field_specs | FieldSpecs가 QuestionBankDoc의 칸·선택지·단위·범위·반복 키를 옮긴 것이다(`build_field_specs`) | 입력과 출력 사이의 변환 관계다 | tests/test_question_fields.py (52칸 대조) |
| extract_request | 직전 응답이 없고 문항 세트에도 현재 문항의 질문 문장이 없으면 거부(`TurnInputError("NO_QUESTION_SENTENCE")`) | session.progress_record.current_question_id를 bank.questions[].question_id에서 찾는 두 입력 사이의 관계다 | 코드의 TurnInputError. 적재 때 문항 세트와 흐름의 일치는 release_loader 쪽 |
| extract_request | ExtractInput.state가 현재 문항 저장 값 -> 직전·참조 문항 저장 값 -> 열린 칸 순서이고 값이 session.answer_records에서 온 것이다(`build_state`) | 입력에서 출력을 만드는 변환이며 삽입 순서 규칙이다 | tests/test_extraction.py |
| extract_request | ExtractInput.question_id가 session.progress_record.current_question_id와 같고 user_utterance가 utterance와 같다(`build_extract_input`) | 입력과 출력 사이의 복사 관계다 | tests/test_extraction.py |
| extract_request | session.progress_record.current_question_id가 flow.order에 있다 | 두 입력 문서 사이의 소속 관계다. 없으면 코드는 직전 문항을 비우고 계속한다(거부 아님) | 코드(previous_item이 None) |
| extraction_check | UNKNOWN_SLOT: output.values[].key가 session.answer_records의 칸 목록에 있다(`check_slots`) | 키 문자열 `Q7_1[soju].amount`를 SlotRef 세 값으로 분해해 다른 입력 배열에서 찾는 관계다. 이 결과가 위반 목록이라 입력 거부가 아니다 | tests/test_extraction.py |
| extraction_check | OUT_OF_CURRENT_ITEM: 저장하는 칸이 현재 문항·next_item과 같은 계열 또는 현재~next_item 사이 문항이다(`check_current_item`) | 입력 문서 세 개(output, session, flow) 사이의 관계와 문자열 계열 계산이다 | tests/test_extraction.py, tests/test_graph.py |
| extraction_check | ExtractionViolations.path의 번호가 output.values의 범위 안이다 | 출력 배열의 번호가 입력 배열의 길이 안인가는 두 문서 사이의 관계다 | 코드 |
| write_plan | 같은 값을 다시 말했으면 갱신에서 뺀다, FIRST_WRITE와 NEW_REVISION의 구분(`plan_writes`) | 입력 session.answer_records와 output.values를 대조하는 변환이다 | tests/test_effects.py, tests/test_extraction.py |
| write_plan | TurnWriteSet.progress_record_update.open_slots의 키가 모두 current_question_id 문항에 속한다 | 문자열 키에서 문항을 뽑아 같은 문서의 다른 값과 비교한다 | 코드(`plan_writes`의 필터), tests/test_extraction.py |
| write_plan | skipped_add와 skipped_remove에 같은 문항이 함께 있지 않다 | 두 배열 사이의 서로소 조건은 JSON Schema로 쓰지 못한다 | 코드(`plan_writes`가 skipped_remove에서 뺀다) |
| write_plan | SKIP/HOLD의 targets가 values의 key 중 하나다(계약 ActionExtraction이 이미 "스키마로 표현 못 함"으로 둔다), HOLD의 사유는 UNCERTAIN으로 고정 | 계약 쪽 의미 검증이며 영역 사이 계약이라 survey-service가 덧붙이지 않는다 | 추론 서버(model-runtime)의 사후 검증(ADR 0010, R7). 서비스는 다시 검증하지 않는다 |
| alert_book | begin_retry가 열린 알림이 아니면 NO_OPEN_ALERT (alert_book.py 216) | 저장된 알림의 현재 상태(OPEN인가)에 달린 조건이라 입력만으로 못 정한다. 결과 쪽은 AlertRead의 `rejected`로 썼다 | `UPDATE ... AND status='OPEN'`의 영향 행 수를 한 트랜잭션에서 본다(저장소 시험) |
| alert_book | 같은 (세션, 턴, 종류)의 알림 재사용(find_by_turn), 같은 키 timeline·signal·subject는 처음 값 유지(ON CONFLICT DO NOTHING) (alert_book.py 106, 270 부근) | 이전에 쓴 행과 비교하는 멱등 규칙이라 상태가 필요하다 | DB 유일 제약과 ON CONFLICT, 저장소 시험 |
| alert_book | 구독 수 한도는 호출자(link_public_api)가 센다. upsert_subscription 자체는 한도를 보지 않는다 (alert_book.py upsert_subscription) | 저장된 구독 개수가 필요하다 | link_public_api의 `_push_add`(아래 행) |
| alert_service | INTERVAL: 마지막 재알림 뒤 retry_min_interval_s(기본 30초) 전의 재알림 거절, retry_after_s는 남은 초 (alert_service.py 216) | 시계와 마지막 재알림 시각(상태)에 의존한다. 거절 모양(INTERVAL이면 retry_after_s >= 1)만 AlertRaised에 썼다 | `retry_staff_call`이 저장소의 `_last_retry_at`과 시계로 판정(알림 서비스 시험) |
| alert_service | RETRY_LIMIT: 세션당 재알림 10회·수기 보고 20회 상한 (alert_service.py 210, 242) | 이미 처리한 동작 수(상태)와 설정값(`SURVEY_STAFF_CALL_*_MAX_PER_SESSION`)이 필요하다 | `store.count_actions`와 설정 비교 |
| alert_service | NO_OPEN_ALERT(재알림은 열린 알림, 보고는 알림 하나라도), OUTCOME_MISMATCH(같은 turn_id의 이전 outcome과 비교) (alert_service.py 204, 235, 239) | 저장된 알림·이전 기록 상태와 비교한다 | `latest_alert`, `get_action` 조회 뒤 판정, 세션별 잠금으로 직렬화 |
| alert_service | 닿은 전달 경로가 없으면 DeliveryFailed (alert_service.py 145) | SSE 연결 수와 푸시 서비스의 접수 여부는 실행 중 상태·외부 응답이다. 출력 모양(`delivery_failed`)만 썼다 | `hub.publish`와 푸시 접수 수의 합이 0인지 본다 |
| release_registry | 적재되지 않은 릴리스의 activate·get은 ReleaseUnavailable (release_registry.py 25, 43) | 레지스트리에 든 ID 집합(상태)에 달렸다. 결과 모양(`unavailable`)만 썼다 | `_loaded` 딕셔너리 조회(레지스트리 시험) |
| release_loader | 매니페스트의 release_id가 포인터의 release_id와 같아야 한다 (release_loader.py 206) | 서로 다른 두 입력 값의 같음은 JSON Schema가 못 쓴다(ReleaseBundle은 `release_id`와 `manifest.release_id`를 따로 받을 뿐이다) | `_check_manifest` 대조(로더 시험), 본 검사는 ReleaseBundle 스키마 통과 뒤에 한다 |
| release_loader | `manifest.runtime_contract_version`이 이 서비스가 지원하는 집합에 있어야 한다(UNSUPPORTED_VERSION) (release_loader.py 209) | 값 하나가 다른 필드(`supported_versions`)의 원소여야 하는 교차 조건이다. `x-survey-schema`는 현재 판만 불러 판별 선택도 못 한다 | `_check_manifest`, 지원 집합은 `survey_schema` 패키지가 정한다 |
| release_loader | 매니페스트 content_hash, 문항 세트의 sha256 해시가 선언된 해시와 같아야 한다(INTEGRITY_ERROR) (release_loader.py 210, 235) | 정규화 직렬화로 해시를 계산해야 하는 값 검사다 | `boundary_contracts.canonical` 계산과 비교 |
| release_loader | `bank.version`이 `manifest.question_set.version`과 같아야 한다 (release_loader.py 237) | 두 필드 사이 같음이라 못 쓴다 | `_check_bank` |
| release_loader | 게이트 판정의 subject와 runtime_contract_version이 매니페스트의 같은 이름 값과 같아야 한다(NO_APPROVAL) (release_loader.py 225) | 두 객체 사이 같음이라 못 쓴다. `result == PASS`는 ReleaseBundle에 썼다 | `_check_decision` |
| release_loader | 판정(GateDecision)을 이웃에서 읽을 수 없으면 NO_APPROVAL, 일시 장애는 재시도 (release_loader.py 219) | 이웃 서비스(I4) 호출 결과다 | 계약 클라이언트의 BoundaryError(code, retryable) |
| release_loader | action_rules_version은 설치본과 같아야 하고 model-runtime은 MAJOR.MINOR가 같아야 한다(VERSION_MISMATCH). PATCH 차이는 경고만 (release_loader.py 249, 259) | 릴리스 값과 설치된 값을 비교하는 교차 조건이다. 해석 가능한 형식(`MAJOR.MINOR`로 시작)만 ReleaseBundle에 썼다 | `_check_library_versions` |
| release_loader | 칸 규격의 코드 중복 등 계약 QuestionFieldSpec 위반(INVALID_FIELD_SPEC) (release_loader.py 241) | 배열 원소 사이 키 유일성은 `uniqueItems`로 쓸 수 없다. 이 판정은 field_specs 컴포넌트의 몫이다(wiring이 `field_check`를 꽂는다) | field_specs 컴포넌트의 출력 FieldSpecIssues가 비어 있는지 로더가 본다 |
| release_loader | 같은 포인터 버전은 다시 읽지 않는다(UNCHANGED), 일시 실패의 백오프(5초 시작, 60초 상한), 상한을 넘으면 RETRY_EXHAUSTED (release_loader.py 83, 120, 140) | 이전 호출의 기록과 시계에 의존한다 | `LoaderStatus.retry`와 시계, 로더 재시도 시험(test_loader_retry) |
| link_public_api | 토큰이 없거나 틀리면 401, 역할이 경로와 안 맞으면 403 (link_public_api.py 186, 240) | 비밀 값 비교(hmac)와 설정된 토큰 집합이 필요하다. `Bearer ` 형식만 ApiRequest에 썼다 | `_role`의 상수 시간 비교 |
| link_public_api | result·risk-source 역할의 토큰을 설정하지 않았거나 알림 저장소가 없으면 그 경로는 404 (link_public_api.py 176, 201) | 서비스 설정에 달린 조건이다(요청에는 없다) | 생성자 인자(설정) |
| link_public_api | 본문이 JSON이어야 하고 같은 키가 두 번 나오면 안 되며 중첩이 너무 깊으면 안 된다(RecursionError) (link_public_api.py 73, 194) | 이 검사는 본문 원문 바이트를 객체로 바꾸는 파싱 단계의 것이고, ApiRequest는 파싱된 뒤의 객체를 받는다 | `_no_dup_keys` 훅과 json 파서 |
| link_public_api | 짝 없는 서로게이트 문자(UTF-8로 인코딩 불가)가 본문에 있으면 400 (link_public_api.py 83, 199) | JSON Schema에는 인코딩 가능 여부를 묻는 키워드가 없다 | `_has_surrogate` (반복 순회) |
| link_public_api | 경로의 session_id와 본문의 session_id가 같아야 한다 (link_public_api.py 317, 447) | 경로와 본문 두 곳의 같음이다. ApiRequest에서 경로 조각과 본문 값을 이어 비교하는 키워드가 없다 | `_message`·`_intervention`의 비교 |
| link_public_api | 발화 길이 상한은 설정값(`SURVEY_API_MAX_UTTERANCE_CHARS`, 기본 1000)이다 (link_public_api.py 122, 327) | 스키마는 기본값 1000을 적었고, 설정으로 바꾼 값은 스키마 파일이 알 수 없다 | `PublicApi.max_chars` |
| link_public_api | subject_ref 필수 여부는 설정(`require_subject_ref`)이 정한다. 세션이 새로 만들어질 때만이다 (link_public_api.py 384) | 서비스 설정과 세션 존재 여부(상태)에 달렸다. SessionStartBody는 선택으로 썼다 | `_start` |
| link_public_api | 세션 상태 조건: 모르는 세션 404, 끝난 세션 409, 멈춘 세션이 아니면 개입 409, 활성 릴리스 없음 503, 저장소 오류 503 (link_public_api.py 380~460) | 세션 저장소와 레지스트리의 현재 상태다 | `_state_of`, 턴 경로의 상태 확인 |
| link_public_api | 세션을 만든 기기만 그 세션에 접근한다(다른 기기는 404) (link_public_api.py 247) | 헤더의 기기 ID와 저장된 소유 기기를 비교한다(상태) | `_device_denied` |
| link_public_api | 같은 turn_id에 다른 입력이면 409, 처리 중이면 409+Retry-After(TURN_IN_PROGRESS) (link_public_api.py 342, 348) | 이전에 저장한 입력·처리 중 표시(상태)와 비교한다. 응답 모양(Retry-After 필수)은 ApiResponse에 썼다 | 오케스트레이터의 멱등 기록(`TurnIdConflict`, `TurnInProgress`) |
| link_public_api | 동시 처리 상한(max_inflight 32) 초과 429, 세션별 상태 조회 빈도 상한(0.5초) 429 (link_public_api.py 203, 472) | 실행 중 동시 요청 수와 시계에 달렸다 | `BoundedSemaphore`와 `_polls` 표 |
| link_public_api | 푸시 구독 수 한도(`SURVEY_MAX_PUSH_SUBSCRIPTIONS`, 기본 100) 초과는 409 (link_public_api.py 753) | 저장된 구독 개수(상태)에 달렸다 | `_push_add`의 `count_subscriptions` |
| link_public_api | 푸시 endpoint가 IP 리터럴이면 공개 주소여야 한다(사설·루프백·링크 로컬·CGNAT 등 거절) (netguard.py `_ip_public`) | IP 주소 범위 판정은 정규식으로 쓸 수 없다. 호스트 이름 형태(https, 사용자 정보 없음, 포트 443, 점이 있는 이름, localhost·내부용 접미사 아님)만 PushSubscribeBody에 썼다. 개발용 `SURVEY_PUSH_ALLOW_PRIVATE_HOSTS`이면 문법 검사 전체를 건너뛴다 | `netguard.endpoint_syntax_error` |
| link_public_api | GET /v1/results의 updated_after가 실제 달력의 날짜여야 한다(예: 2026-13-45 거절) (link_public_api.py 695) | 정규식은 모양만 보고 월·일 범위와 윤년을 판정하지 못한다 | `datetime.fromisoformat` |
| link_public_api | 접근 기록을 남기지 못하면 환자 데이터를 주지 않고 503(fail-closed) (link_public_api.py `_audit`) | 저장소 쓰기 성공 여부다 | `_audit`가 StoreError를 잡아 503 |
| link_public_api | ApiResponse의 성공 본문(body)이 body_type이 가리키는 계약·문진 스키마를 따라야 한다 | `x-contract`·`x-survey-schema`를 쓰는 현 검증기(`validation._walk`)는 `properties`·`items`만 내려가고 `oneOf`·`allOf`·`$ref` 안은 보지 않아, body_type마다 다른 스키마를 고르는 위임을 쓸 수 없다 | 내는 쪽이 내기 전에 그 계약·문진 스키마로 검증한다(R7). 검증기가 `if/then`을 내려가게 되면 스키마에 옮긴다 |
| alert_book, alert_service, release_registry, link_inference | AlertRead·AlertRaised·ReleaseEntry·ReleaseLookup·AlertWrite·InferenceResult의 `oneOf` 가지 안에 단 `x-contract`(StaffAlert, StaffCallRetryResult, ReleaseManifest, ActionExtraction 등)·`x-survey-schema`(QuestionBank, RiskSignal) | 위 행과 같은 이유로 현 검증기는 가지 안의 주석을 위임하지 않는다(주석은 문서로만 작동한다). 최상위 속성에 단 위임(ActivePointer, ReleaseBundle, ApiResponse.error, InferenceCall, PushDelivery, OutboxEnvelope)은 동작한다 | 내는 쪽의 계약 검증. 검증기 확장 때 자동으로 켜진다 |
| link_boundary_clients | 토큰 환경 변수 이름이 `*_TOKEN_ENV`로 바뀌면 그 이름의 `_FILE`·값이 있어야 한다 (link_boundary_clients.py 24) | 필요한 키의 이름이 다른 키의 값으로 정해져서 JSON Schema로 못 쓴다. 기본 이름일 때의 존재 조건은 썼다 | `read_secret`이 SecretError를 내고 `_make`가 ConfigError로 바꾼다 |
| link_boundary_clients | 토큰 파일이 읽히고 비어 있지 않아야 한다 (boundary_contracts/secrets.py) | 파일 시스템 상태다. 값은 스키마·로그에 둘 수 없다(D84) | `read_secret` |
| link_trace_export | 봉투의 `trace`는 `runtime_contract_version`이 가리키는 판의 TurnTrace를 따라야 한다. 이 서비스에 없는 판이면 LOCAL_INVALID (link_trace_export.py 55) | 다른 필드의 값으로 스키마 판을 고르는 일은 못 쓴다. OutboxEnvelope는 현재 판의 TurnTrace만 위임한다 | `_local_errors`가 판별 검증기를 고른다 |
| link_trace_export | FlushResult의 합이 이번에 꺼낸 행 수와 같다, 불능 때 나머지를 쉰다 (link_trace_export.py 80~118) | 이번 주기의 입력 행 수(상태)와 합 비교가 필요하다. 개별 필드 범위와 retried -> paused 함의만 썼다 | `flush_once` |
| link_trace_export | 일시 불능(UNAVAILABLE)일 때만 재시도, 지수 백오프(1초 시작, 60초 상한), 그 밖의 실패는 DEAD (link_trace_export.py 96) | 이웃 응답과 시계에 달린 규칙이다 | `BoundaryError.retryable` 판정과 `pause_until` |
| link_inference | 추론 서버가 릴리스를 잊었으면(404) 한 번 다시 적재, 읽기 타임아웃은 재시도하지 않고, 남은 턴 예산이 호출 상한 이상일 때만 재시도 (link_inference.py 110, 135~158) | 이웃 응답, 시계, 남은 예산(deadline - now)에 달린 조건이다. deadline 값은 InferenceCall에 담았다 | `RemoteModelSession._call` |
| link_inference | extract_input이 가리키는 문항이 flow·fields에 있어야 한다(세 값 사이의 일관성) | 세 객체를 가로지르는 조건이다. 서버가 flow·fields로 출력 규칙을 항상 보증하고 받는 쪽은 다시 검사하지 않는다(R7, 계약 3.0.0) | 추론 서버(model-runtime)와 extraction_check 컴포넌트 |
| link_push | 보낼 때 endpoint의 호스트를 해석해 모든 주소가 공개 주소여야 한다(아니면 FAILED) (link_push.py 38) | DNS 해석 결과(네트워크 상태)다 | `netguard.host_is_public` |
| link_push | 푸시 서비스 응답이 404·410이면 GONE, 접수는 ACCEPTED, 그 밖은 FAILED (link_push.py 49) | 외부 서비스의 응답이다. 결과 값의 집합만 PushOutcome에 썼다 | `WebPushSender.send` |
| link_push | VAPID 비밀키 형식과 subject(mailto:·https://) (link_push.py 19~27) | 비밀 값은 스키마·설정 파일에 두지 않는다(D84). 생성자 인자라 PushDelivery 입력에 없다 | 시작 때 `Vapid.from_*`와 `ValueError` |
