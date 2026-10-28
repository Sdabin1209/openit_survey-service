# survey-service 구조 규칙 R1~R7 전수 점검 보고서 (2026-10-28)

점검 기준: `head/docs/audit/structure-rules-audit-spec.md`(D104) A~G. 대상: 이 리포 `survey_service/`(약 6,800줄), `schema/`, `scripts/`, `Dockerfile`, 문서. 점검 방식은 읽기뿐이며 코드·설정은 고치지 않았다. 계약 대조는 `../boundary-contracts`의 INTERFACES.md(I4·I6·I8·I9·I10), HTTP.md(I4·I6·I9·I10), PUBLIC_API.md, CONVENTIONS.md를 읽어 했다.

표기: **[확인]** 파일에서 읽은 것, **[판단]** 읽은 것에서 내가 낸 해석(규칙 적용), **[미확인]** 읽지 못했거나 실행하지 않은 것.

## 0. 요약

| 심각도 | 개수 | ID |
|---|---|---|
| P1 | 6 | SV-01, SV-02, SV-03, SV-04, SV-05, SV-14 |
| P2 | 11 | SV-06, SV-07, SV-08, SV-09, SV-10, SV-11, SV-12, SV-13, SV-15, SV-16, SV-17 |
| P3 | 3 | SV-18, SV-19, SV-20 |

(ID는 절 순서가 아니라 발견 순서로 붙었다. 합계 20건.)

실행한 것: 이 리포 시험 중 `test_public_api.py`, `test_public_api_v132.py`, `test_staff_call.py`, `test_results_signals_retention.py`, `test_inference_client.py`, `test_trace_export.py`, `test_release_loader.py`, `test_graph.py`, `test_extraction.py`, `test_schema_package.py`(.venv, 실패 0, PostgreSQL이 필요한 일부는 skip). 이 시험은 계약 패키지의 `conformance.servers` 참조 서버와 클라이언트를 쓴다(design.md 19.3). 전체 시험, PG 시험, 이미지 스모크는 돌리지 않았다.

주의할 사실(요청 단서 정정): `turn_engine`을 런타임에서 import하는 파일은 **3개**(`flow.py:13`, `graph.py:27`, `service.py:20`)다. "9개"는 시험·스크립트·주석 언급까지 센 값으로 보인다. 문자열 언급만 있는 곳: `orchestrator.py:5`, `cli.py:4`, `release/loader.py:240-243`(버전 문자열 키). 시험에서 import: `tests/test_release_loader.py:6`, `tests/http_support.py:5`, `tests/test_extraction.py:11`, `tests/fakes.py:110`.

## A. 컴포넌트 목록

기준 목록은 `schema/COMPONENTS.yaml`(Frontend, Staff Console, Orchestrator, Action Extractor, Extraction Validator, Response Composer, Result Dispatcher, Question Bank, Session Store)이다. 아래는 **코드에 실제로 있는** 작업 단위다. "할당 I/O"는 입출력 스키마가 기계가 읽는 형태로 지정된 것을 말한다.

| # | 컴포넌트 | 파일 | 입력 스키마 | 출력 스키마 | 업무 | 할당 I/O |
|---|---|---|---|---|---|---|
| 1 | Orchestrator(`handle_turn`, 락, 마감, 멱등) | `orchestrator.py` | `FrontendMessage`/`InterventionDecision`(+`turn_id`) | `TurnOutcome`(dataclass), `FrontendInput`, `TurnTrace` | 한 턴 진입, 세션 락, 총 마감, 같은 턴 ID 재생 | 부분(입출력 계약 있음, 내부는 `Orchestrator` 속성 전체) |
| 2 | 턴 그래프(LangGraph 노드 19개) | `graph.py:205-207` | `TurnState`(TypedDict, `graph.py:88`) | `TurnState` | prescreen, load_session, col_none/progress/paused/ended, ignore, fail_no_session, bind_release, extract, validate_extraction, plan_writes, plan_other, plan_risk, commit, deliver_staff, dispatch_result, respond, finalize | 노드별 I/O 없음. 전 노드가 하나의 `TurnState`(약 50키)와 `self.o`(Orchestrator)를 공유 |
| 3 | Session Store | `store/base.py`(Port), `sqlite.py`, `postgres.py`, `memory.py`, `effects.py` | `SessionLoadRequest`/`SessionCommitRequest`(문진 스키마 `SessionStore.json`), 커밋 쓰기 `TurnWriteSet` | `SessionLoadResult`/`SessionCommitResult` | 세션 문서 JSON + revision, 턴 멱등 기록 | Port는 Protocol(`store/base.py`), 값은 dict |
| 4 | 릴리스 로더 + 레지스트리 | `release/loader.py`, `registry.py` | I4 `ActivePointer`, `ReleaseManifest`, `ReleaseBundle`, `GateDecision` | `LoadedRelease`(dataclass, `ports.py:20`) | 포인터 폴링, 검사, 적재, 거부 시 이전 유지 | 있음(계약 + dataclass) |
| 5 | 추론 클라이언트 | `remote.py`, `inference.py`(프로세스 내 개발용) | `ActionExtractionInput`/`ResponseRequest` | `RuntimeResult`(`boundary_contracts.clients`) | I9 HTTP 호출, 통계, 재적재 | 있음(계약) |
| 6 | 직원 알림(저장·SSE·푸시·재알림) | `alerts.py`(770줄: `AlertStore`, `AlertHub`, `WebPushSender`, `AlertService`, `AlertStaffConsole`, `PgAlertStore`) | `StaffConsoleInput`, `StaffAlert`(문진/계약 스키마) | `StaffAlert`, `StaffAlertPage` 등 | 알림 영속, 전달 판정, 구독, 재알림, 세션 메타·접근 기록·시각·신호 저장 | 일부(아래 SV-14) |
| 7 | 위험 규칙 | `risk_rules.py` | 발화 문자열, 규칙 파일 | `StaffAlert` dict | 규칙 감지(임시 목록 포함) | `RiskSource` Protocol(`ports.py:46`)은 `check`만 선언, 실제는 `screen`·`utterance_only`도 쓴다(SV-08) |
| 8 | A 입력 조립 · 쓰기 계획 · 값 검사 · B 입력 조립 | `extraction.py`(252줄), `flow.py`, `assembly.py` | `ActionExtractionInput`/`ActionExtraction`, `SessionStore` 문서 | `TurnWriteSet`, `ResponseRequest`, `Violation` | 세 컴포넌트(A 입력 / 저장 계획 / B 입력)가 한 모듈 | 경계 없음(SV-10) |
| 9 | 표 찾기 | `response_tables.py`(501줄), `schema/data/response_tables.json` | `ResponseTables`(문진 스키마) | B 입력 문장 필드 | 데이터셋 담당 규칙을 옮긴 문장 조립 | 데이터는 계약, 함수는 모듈 함수 |
| 10 | Trace 조립·outbox·전송 | `trace/exporter.py`, `outbox.py`, `pg_outbox.py` | `TurnTrace`, `TraceEnvelope` | `IngestResult` | 영속 outbox, 비동기 전송 | 있음 |
| 11 | 공개 API | `api.py`(771줄) + `service.py`의 HTTP 핸들러 | HTTP 본문, 문진 스키마 `FrontendMessage` 등 | `FrontendInput`, `SessionState`, 오류 | I10 라우팅·인증·검증 | 있음(PUBLIC_API.md) |
| 12 | 보존 정리 | `retention.py` | `Settings` 보존 일수 | 삭제 건수 dict | 주기 정리 + `purge` CLI | 입력은 설정, 출력 dict |

접착 코드(어느 컴포넌트에도 속하지 않음): `service.py`(`App` 조립, `/healthz` `/readyz`, SSE 스트림, 로깅, 시그널, 576줄), `settings.py`, `db.py`, `pg.py`, `netguard.py`, `errors.py`, `ports.py`, `clients.py`, `cli.py`, `__main__.py`.

**컴포넌트 경계가 없는 덩어리**: `graph.py`(894줄)는 컴포넌트 3개(턴 그래프, 위험 판정, 직원 전달)의 일을 같이 한다. `extraction.py`는 세 컴포넌트가 한 파일이다. `alerts.py`의 `AlertStore`는 알림 외에 세션 메타·접근 기록·시각·위험 신호·구독·기기 소유를 한 클래스에 둔다. 이 셋은 "경계가 없다"고 쓴다.

## B. R3·R4 (독립성, 자기 I/O만 안다)

### SV-01 [R3·R7, P1] extract와 validate_extraction이 상태 키로 서로를 다시 실행시킨다
- 위치: `graph.py:114`, `graph.py:221-222`, `graph.py:495`, `graph.py:527-529`
- 인용: `return {"extract_next": "extract", "violations": rec, "extract_out": None, "trace": tr}` / `upd["extract_next"] = "extract"  # D75: 같은 요청을 1회 재시도`
- 왜 위반: 검증 노드가 `extract_next`·`extract_calls`·`extract_in`을 직접 만져 추출 노드를 다시 돌린다. 두 노드가 `extract_next` 값 어휘(`validate|extract|finalize|plan`, 두 간선 함수가 서로 다른 어휘를 쓴다: `graph.py:257`은 "validate", `graph.py:261`은 "plan")와 `extract_calls` 카운터 의미를 함께 안다. 노드의 출력 키가 다음 노드의 제어 흐름을 정한다(R3). 같은 일의 R7 쪽 이유는 SV-02.
- 최소 수정: 재시도를 없애면(SV-02) 이 결합도 사라진다. 503 OUTPUT_INVALID 재시도는 값이 나오지 않은 호출의 재시도라 `extract` 노드 안(호출 클라이언트)으로 내리면 `extract_next`가 필요 없다.

### SV-04 [R4, P1] Orchestrator 노드가 다른 컴포넌트의 판단(위험 규칙, 되묻기 상한)을 직접 한다
- 위치: `graph.py:559-566`(`_slot_risk` 호출), `graph.py:576-604`(`_slot_risk`), `graph.py:606-627`(`_reask_escalation`), `graph.py:273-320`(`n_prescreen`)
- 인용: `rule = (slot or {}).get("risk_rule")` / `if rule["operator"] == "=": hit = val == ref_val and isinstance(val, bool) == isinstance(ref_val, bool)` / `for e in reversed(st["state_doc"]["dialogue_log"]): ... ex["action"] not in ("REASK", "CONFIRM")`
- 왜 위반: 설계 자신이 "Orchestrator는 판단하지 않는다"(design.md 12절 표, graph.py 독스트링)고 쓰는데, 노드가 QuestionBank 슬롯의 `risk_rule` 연산자 의미와 대화 로그의 `extract_output` 모양을 알고 판정한다. 위험 컴포넌트(`risk_rules.py`, `RiskSource` Protocol)가 따로 있는데 `_slot_risk`는 포트를 거치지 않는다. 같은 정지 쓰기(`session_status=PAUSED_RISK`, `pause_history_add`)를 `n_plan_risk`(graph.py:342-348), `n_plan_writes`(graph.py:554-573), `Orchestrator.apply_risk_signal`(orchestrator.py:197-206) 세 곳이 각자 조립한다(같은 규칙의 재구현).
- 최소 수정: `_slot_risk`·`_reask_escalation`을 위험 컴포넌트(입력 `ActionExtraction`+문서 요약, 출력 `StaffAlert`/정지 사유)로 옮기고 정지 쓰기 조립은 한 함수로 합친다.

### SV-05 [R3, P1] 노드 사이의 숨은 통로: contextvars와 알림 ID 파싱
- 위치: `graph.py:333-340`, `alerts.py:114-117`, `alerts.py:582-583`, `alerts.py:744`, `orchestrator.py:213`
- 인용: `tok = CURRENT_TURN.set((sid, turn_id)); tok2 = CURRENT_PAUSE_REASON.set(pause_reason)` (graph) / `pause_reason = pause_reason or CURRENT_PAUSE_REASON.get()` (alerts) / `signals.record_signal(sid, signal, int(a["alert_id"][3:]), status)`
- 왜 위반: `StaffConsole.deliver(console_input)`의 입력 스키마(`StaffConsoleInput`)에는 턴 ID와 정지 사유가 없다. 그래서 그래프가 전역 `ContextVar`를 설정하고 `alerts.py`가 읽는다. 알림 멱등 키와 정지 사유가 입출력 스키마 밖의 전역 상태로 건너간다. `graph.py:333`은 노드 안에서 `alerts` 모듈을 지연 import해 모듈 결합도 만든다. `alert_id`의 `"alr"`(3글자 접두) 형식을 `orchestrator.py:213`이 안다(알림 컴포넌트의 ID 형식이 새어 나옴).
- 최소 수정: `StaffConsoleInput`(survey 스키마)에 `turn_id`, `pause_reason`을 선택 필드로 추가하거나 `deliver(console_input, *, context)`로 명시 전달. `alert_id`는 불투명 값으로 `record_signal`이 숫자를 직접 받지 않게 한다.

### SV-06 [R3·R4, P2] 모든 노드가 `self.o`(Orchestrator)로 모든 자원에 닿는다
- 위치: `graph.py:199-200`, 예 `graph.py:298`(`self.o.store.load`), `graph.py:282`(`self.o.releases.active()`), `graph.py:443`(`self.o.flow.table.order[0]`), `graph.py:748`(`self.o.tables`), `graph.py:269`(`self.o._count` 호출 다수, 비공개 이름)
- 왜 위반: 노드 입력이 `TurnState`뿐이 아니라 Orchestrator 속성 전체다. 노드가 어떤 자원을 쓰는지 선언이 없다. `_count`(비공개)와 `Orchestrator.counters`를 노드가 직접 쓴다.
- 최소 수정: 노드를 만들 때 필요한 포트만 주입(클로저/생성자 인자)한다.

### SV-07 [R3·R4, P2] 한 모듈이 세 컴포넌트의 일을 하고, 서비스가 모델 출력을 다시 가공한다
- 위치: `extraction.py:79`(A 입력), `extraction.py:175`(저장 계획), `extraction.py:240`(B 입력), `extraction.py:119`(값 검사)
- 왜 위반: A 입력 조립과 B 입력 조립은 서로 모른 채 같은 `state`·`ActionExtraction`을 읽는다. `build_response_request`는 `extract_input["state"]`(A 입력의 내부 모양)를 B 입력 조립에 다시 쓴다(`extraction.py:~245`, `tables.b_fields(out, extract_input["question_id"], extract_input["user_utterance"], extract_input["state"])`). 즉 B 조립 컴포넌트가 A 입력 컴포넌트가 만든 출력 모양에 기댄다.
- 최소 수정: 파일을 컴포넌트별로 나누고, B 조립의 입력에 필요한 값만 명시 스키마로 받게 한다.

### SV-08 [R3, P2] 포트 Protocol에 없는 속성에 기대는 덕 타이핑
- 위치: `graph.py:278`, `graph.py:283`, `graph.py:543`(`getattr(src, "utterance_only", False)`, `src.screen(utt, extra)`), `graph.py:706`(`getattr(self.o.dispatcher, "attached", True)`), `ports.py:46-50`(Protocol은 `check`만), `ports.py:65`(`attached = False`는 구현에만)
- 왜 위반: `RiskSource` Protocol은 `check(...)`만 말하지만 그래프는 `utterance_only`, `screen`을 부른다. `ResultDispatcher`의 `attached`도 Protocol 밖이다. 암묵 스키마.
- 최소 수정: 두 속성을 Protocol에 선언하거나 포트를 둘로 나눈다(발화용/추출 결과용).

### SV-09 [R3, P2] 공개 API가 두 저장소 내부 메서드에 직접 닿는다
- 위치: `api.py:241`, `api.py:319`, `api.py:341`, `api.py:608-610`, `api.py:620`, `api.py:683`, `api.py:704`(`apply_risk_signal(..., alerts=self.alerts, signals=self.alerts.store)`)
- 인용: `self.alerts.store.timeline(sid)`, `self.orch.store.list_sessions(...)`
- 왜 위반: API 컴포넌트가 Orchestrator의 `store`, 알림 컴포넌트의 `store`를 속성으로 꺼내 쓰고, 세션 문서 내부(`dialogue_log[i]["input"]["utterance"]`, `progress_record.pause_history`: `api.py:468-475`, `api.py:605-617`)를 직접 해석한다. `apply_risk_signal`은 `alerts`·`signals`를 duck typing으로 받는다(orchestrator.py:171-175 독스트링이 메서드 이름만 적는다).
- 최소 수정: 조회용 읽기 포트(`SessionReadPort`)를 정의하고 API는 그것만 받는다.

## C. R5 (영역 안 컴포넌트 사이 스키마)

[확인] 정의 위치: 컴포넌트 사이 값 대부분은 `schema/schemas/*.json`(JSON Schema)과 `store/base.py`·`ports.py`(Protocol, dataclass)에 있다. 그래프 상태는 `graph.py:88`의 `TypedDict`다.

### SV-10 [R5, P2] `TurnState`는 타입이 있지만 내부 모양이 암묵이다
- 위치: `graph.py:88-143`
- 인용: `route: dict`, `writes: Optional[dict]`, `resp: Optional[dict]  # 응답 계획 {kind: model_start | model | fixed | none, text?}`, `console: dict`, `trace: Annotated[dict, _merge]`
- 왜: 최상위 키 이름은 TypedDict로 기계가 읽지만, 노드 사이에서 실제로 건네는 값 대부분(`route`, `writes`, `resp`, `alert`, `console`, `signal`, `violations`)은 모양이 `dict`와 주석뿐이다. `resp.kind`의 닫힌 집합, `extract_next` 어휘(SV-01)가 코드 관습이다. 어느 노드가 어떤 키를 쓰고 읽는지 표가 없다. 예: `n_deliver_staff`는 `alert`, `alert_sent`, `intervention_result`, `console`, `route`를, `n_respond`는 `resp`, `extract_out`, `extract_in`, `post_doc`를 다른 노드가 만들었다고 가정한다. `design.md` 14.4는 "상태는 JSON 직렬화가 가능"만 시험한다(`json.dumps`).
- 최소 수정: `Plan`, `Resp`, `ConsoleCall` 등을 TypedDict/JSON Schema로 정의하고 노드별 읽기/쓰기 키를 표(또는 데코레이터)로 선언한다.

[확인] 판정: 문진 스키마(`schema/`)의 분류는 아래 E절 SV-14.

## D. R1 (이웃 영역 구조에 기대는 곳)

허용된 읽기 참조는 `schema/`(자기 것), `boundary-contracts`, `head/shared/areas.json`뿐이다. 이 리포 안에서 확인한 방법: `grep -rn '\.\./\|deploy/\|head/\|ops-feedback\|release-gate\|kiosk'`(scripts, Dockerfile, pyproject, requirements, tests/compose)와 `grep "^from \|^import "`(survey_service 전체).

[확인] 이웃 패키지 import는 `boundary_contracts`, `turn_engine`, `model_runtime`(규칙이 허용한 라이브러리)뿐이다. 이웃 영역 리포 코드 import 없음.

### SV-11 [R1, P2] 스모크·부하 스크립트와 문서가 `deploy` 폴더와 컨테이너 이름에 기댄다
- 위치: `scripts/smoke.sh:157`, `scripts/smoke.sh:162`, `scripts/load_test.py:8`, `docs/boundary.md:87`
- 인용: `DEVPG="$PWD/../deploy/scripts/dev-postgres.sh"; [ -f "$DEVPG" ] || fail ...` / `$DOCKER inspect ... "deploy-devpg-survey-$DEV_PG_SUFFIX"` / boundary.md: "deploy `compose.yaml`이 이 서비스에 주는 것을 읽어 대조, 2026-10-28"
- 왜 위반: 옆 폴더 경로(`../deploy/...`), 이웃이 정한 컨테이너 이름 규칙(`deploy-devpg-<이름>-<접미>`), 이웃 스크립트의 stdout 계약(접속 URL 출력)을 안다. 워크스페이스 규칙은 읽기 허용을 3곳으로 한정한다. boundary.md는 deploy의 compose를 읽었다고 쓰고 그 내용(비밀 `_FILE` 이름 8개)을 사본으로 갖는다("정본은 compose이고 이 목록은 대조 시점의 사본", 같은 한 사실 한 곳 규칙에도 어긋남). `SMOKE_PG=1`이 선택이라 기본 경로는 영향이 없다.
- 최소 수정: PG 스모크를 `compose.dev.yaml`(자기 리포)로 옮긴다(공통 규칙 D22). boundary.md의 비밀 이름 목록은 `settings.py`가 읽는 이름만 쓰고 deploy 문서를 링크한다.

### SV-12 [R1, P2] 데이터셋 담당(학습 서버)의 내부 구현과 경로에 기대는 표 규칙
- 위치: `docs/design.md:231`, `docs/design.md:237`, `survey_service/response_tables.py`(전체 501줄), `schema/data/response_tables.json`, `scripts/reference_agreement.py:4`, `docs/boundary.md:75`
- 인용: design.md 18절 "학습 서버 `~/WS/dataset/scripts/labels.py`(2026-10-14)의 `topic`, `short_topic`, `value_text`, `_group_texts`, `ask_text`, ..., `b_input`이 학습 데이터 B 입력의 생성기"
- 왜 위반: B 입력의 문장 규칙을 이웃(데이터셋 담당)의 비공개 구현에서 옮겼고, 그 규칙을 계약이 아니라 "원본과 글자 단위 일치"(boundary.md:76 "32,289행 일치")로 따른다. 승인 기록이 없다(`table_status: DATASET_RULES_UNCONFIRMED`). 이웃 구현이 바뀌면 이 영역이 조용히 어긋난다. 시험·스크립트는 학습 서버 데이터 경로를 환경 변수로 받는다.
- 최소 수정: B 입력 필드 의미를 boundary-contracts(`ResponseRequest`의 필드 의미 절)로 올리거나 데이터셋 담당이 배포하는 표를 받는 계약을 헤드가 정한다(SV-15).

### SV-13 [R1, P2] model-runtime·boundary_contracts 클라이언트의 내부에 기댄다
- 위치: `release/loader.py:257`, `inference.py:45`, `remote.py:100`
- 인용: `from model_runtime.prompts import DEPRECATED_TEMPLATE_VERSIONS, DEFAULT_TEMPLATE_VERSION` / `m = re.search(r"<\|im_start\|>user\n(.*)<\|im_end\|>", req.prompt, re.S)` / `_timed_out(e, elapsed, self._rt._timeout)`
- 왜 위반: (1) 공개 API가 아닌 `model_runtime.prompts` 모듈 상수를 직접 import(INTERFACES.md I9의 서명 목록에 없다). (2) `dev_responder`가 model-runtime의 프롬프트 조립 형식(ChatML 마커)을 파싱한다. (3) `HttpModelRuntime._timeout` 비공개 속성을 읽는다. 세 곳 모두 이웃 쪽 내부 변경에 조용히 깨진다. (1)은 `ImportError`를 삼켜 목록이 없는 옛 설치본에서는 경고를 건너뛴다(`loader.py:258-259`).
- 최소 수정: (1)은 I9 `validate_serving_config` 같은 공개 함수로 요청(변경 요청). (3)은 클라이언트에 `timeout` 공개 속성을 요청. (2)는 개발 백엔드를 `FakeBackend`의 입력 객체에서 읽는 방식으로 바꾼다.

## E. R6 (영역 사이 스키마)

### 소비·제공 인터페이스와 계약 위치

| 인터페이스 | 방향 | 계약 위치 | 비고 |
|---|---|---|---|
| I4 ReleaseReader | 소비 | INTERFACES.md I4, HTTP.md I4, 스키마 `ReleaseManifest`, `ActivePointer`, `GateDecision` | 있음 |
| I6 TraceIngest | 소비(송신) | INTERFACES.md I6, HTTP.md I6, `TraceEnvelope`, `IngestResult` | `trace` 본문 `TurnTrace`는 survey 스키마 |
| I8 turn-engine | 소비(라이브러리) | INTERFACES.md I8 | `QuestionFlowTable` 생성 형식은 계약에 없음(SV-15) |
| I9 model-runtime | 소비 | INTERFACES.md I9, HTTP.md I9, `ActionExtraction(Input)`, `ResponseRequest`, `ResponseText` | 있음 |
| I10 공개 API | 제공 | PUBLIC_API.md, HTTP.md I10, `FrontendMessage` 등은 survey 스키마 | 있음 |

### SV-14 [R5/R6 경계, P1] 영역이 소유하지만 영역 경계를 넘는 문진 스키마, 같은 정수 버전 안의 비호환 변경
`schema/schemas/`의 16개를 소비자 기준으로 나눈다([확인]: `grep`으로 boundary-contracts 안의 참조를 보고, PUBLIC_API.md·INTERFACES.md 본문을 읽은 결과).

| 구분 | 스키마 | 근거 |
|---|---|---|
| 영역 안(R5, survey-service 책임) | `SessionStore`, `OrchestratorInput`, `OrchestratorOutput`, `StaffConsoleInput`, `DispatchInput`, `DispatchOutput`, `QuestionFlow`, `ResponseTables` | 서비스 안 컴포넌트 사이에서만 오간다. `DispatchInput/Output`은 결과 전달 대상이 붙으면 경계가 된다(지금 NO_TARGET) |
| 영역 경계를 넘음(R6, 헤드 책임이어야 함) | `FrontendMessage`, `FrontendInput`(→`TurnFailureSignal` 포함), `InterventionDecision`, `StaffSessionView`, `SessionResult`, `RiskSignal`, `TurnTrace`, `QuestionBank` | PUBLIC_API.md가 "스키마 원문은 문진 스키마" 또는 "survey-service 문진 스키마에 둔다"고 가리키고(PUBLIC_API.md 54·105·133줄 근처, H절), `TurnTrace`는 ops-feedback이 읽고(`TraceEnvelope.trace`), `QuestionBank`는 question-authoring이 쓰고 이 영역이 읽는다 |

- 위치: `docs/design.md:222`
- 인용: "같은 정수 버전 안에서 v1 모델 입출력 계약 12개(`UtteranceParserInput` 등)를 지우고 `SessionStore`, `TurnTrace` 등을 바꾸고 ... **이것은 같은 정수 버전 안의 비호환 변경이다**(ops-feedback이 가진 TurnTrace 사본, 결과 전달의 DispatchInput)"
- 왜 위반: 경계를 넘는 스키마를 소유 영역이 정수 버전 증가 없이 비호환으로 바꿨다고 기록한다(R6: 영역 사이 스키마는 헤드가 책임). 이후 `pause_reason`(PUBLIC_API.md H절)도 "survey-service가 자기 스키마에 반영한다"로 처리돼, 계약 문서가 이 영역 파일을 정본으로 가리킨다. 이 보고서 작성 시점에 해소됐는지 [미확인]. 정수 버전을 올릴지는 헤드 결정이라고 design.md가 쓴다.
- 최소 수정: 경계를 넘는 위 8개를 boundary-contracts로 옮기거나, survey 스키마 변경을 헤드가 승인하는 절차(변경 요청 → 계약 버전)로 고정한다. 이 영역은 변경 요청 문서를 이미 쓰는 중이다(`docs/change-requests/CR-013`, `CR-017`).

### SV-15 [R6, P2] 영역 경계를 넘는데 계약에 없는 형식
1. **`QuestionFlow` → `turn_engine.QuestionFlowTable`** (질문 6): `flow.py:43` `QuestionFlowTable(order=data["order"], skips=[SkipRule(s["slot"], s["value"], s["skip_item"]) for s in data["skips"]])`. INTERFACES.md I8은 "`QuestionFlowTable`는 문진 스키마에서 만든 읽기 전용 표 ... 이 계약은 표의 형식을 정하지 않는다"고 쓴다. 변환은 (a) turn-engine의 파이썬 생성자 시그니처(`order: tuple, skips: tuple`, `SkipRule(slot, value, skip_item)`), (b) 문진 스키마 `QuestionFlow`의 필드가 서로 일치한다는 암묵 약속이다. `QuestionFlow.skips[].op`(const `"=="`)는 `flow.py:43`에서 읽지 않고 버린다. `==` 외 연산은 스키마가 거부한다(`QuestionFlow.json`의 `op: {const: "=="}`). 바꾸려면 turn-engine, 문진 스키마, 이 변환 세 곳이 동시에 바뀐다. 변환 함수 `build_flow`는 이 영역에 있다.
2. **A 입력 `state`의 순서 규칙**(`reference_sources`, `slot_order`, `extraction.py:46-76`): 모델이 학습된 분포에 맞추는 표본 유래 규칙(design.md 17절: 13/549 불일치, "미확인")이다. `ActionExtractionInput`의 계약(JSON Schema)은 순서를 말하지 않지만 model-lab·model-runtime의 학습/직렬화와 일치해야 한다. 계약 밖 약속.
3. **B 입력 문장 규칙**(SV-12): 데이터셋 담당과의 약속, 계약 없음.
4. **`/readyz` 본문의 모양**: `docs/boundary.md:73`이 "`/readyz`에서 `trace_outbox.dead_by_code`, `staff_alert_source.provisional`, `kiosk_tokens.shared_kiosk_token` ... 이 0이 아니면 알린다"며 deploy 경보의 입력으로 쓴다. CONVENTIONS.md는 `/readyz`의 200/503과 2초 마감만 정한다. 본문 필드는 계약이 아니다.
5. **환경 변수와 비밀 파일 이름**(`SURVEY_*_FILE`, boundary.md:87)과 **`python -m survey_service purge`**, **이미지 라벨 `io.openit.langgraph`**(design.md 14.4: 공통 6개 키 밖의 추가 키): deploy와의 약속이며 계약 문서에 없다. 라벨은 CR-011.
6. **추론 서버 호출 실패 처리의 정본이 이 영역 문서**: HTTP.md I9 마지막 줄이 "서비스의 실패 처리 ... survey-service의 `inference-server-spec.md` 5절을 따른다"고 쓴다. 계약이 영역 문서를 가리킨다(inference-server-spec.md 자체는 상단에 "[D74로 대체됨]"과 "기준은 HTTP.md"라고 써서 서로를 가리킨다).
7. **세션 ID 외부-내부 변환**(`x-`+SHA-256 앞 32자): CONVENTIONS.md 10절에 있으므로 계약 안이다(확인).

- 최소 수정: 1은 I8에 `QuestionFlowTable` 구성 인자를 명시(변경 요청). 2·3은 계약 또는 데이터 계약으로 승격. 4는 `/readyz` 경보 필드를 계약에 두거나 deploy가 쓰는 필드만 목록으로 고정. 6은 HTTP.md가 실패 처리 요구를 직접 담는다.

### SV-16 [R6, P2] 계약의 구멍: 문항 세트만 받을 방법이 없어 어댑터 번들 전체를 내려받는다
- 위치: `release/loader.py:185`, `remote.py:138`
- 인용: `bundle = self.reader.load_bundle(rid, dest=self.workdir)` / `def load(self, manifest: Mapping, adapters, *, contract_version=None) -> RemoteModelSession:` (adapters를 쓰지 않음)
- 왜: INTERFACES.md I4에서 문항 세트(`question_bank`)를 얻는 유일한 호출이 `load_bundle`이고, 이는 어댑터 디렉터리까지 내려받고 해시를 검증한다. HTTP.md I9는 추론 서버가 번들을 스스로 읽는다고 한다("서비스는 어댑터를 보내지 않는다"). 원격 모드에서 서비스가 받은 어댑터는 `RemoteInference.load`가 버린다. 같은 번들을 두 곳이 읽는다. boundary.md §5 A8도 이 비용을 "알려진 한계"로 적는다. 호출 규약 자체는 지켰으므로 위반이 아니라 계약 공백이다.
- 최소 수정: I4에 문항 세트만 읽는 호출(예: `get_question_bank(release_id)`)을 추가하는 변경 요청.

## F. 계약 준수 (한 줄씩)

계약 패키지의 적합성 시험: 이 리포 시험이 계약 참조 서버(`conformance.servers`, 사본)와 `HttpModelRuntime`·`HttpReleaseReader`·`HttpTraceIngest`·`HttpPublicApi` 클라이언트를 쓰므로 위 10개 시험 파일을 돌렸고 통과했다. 계약 저장소 자체의 `tests/`는 돌리지 않았다([미확인]).

### F-1. 추론 호출 I9 (HTTP.md I9, INTERFACES.md I9)
| 계약 | 구현 | 판정 |
|---|---|---|
| `PUT /v1/releases/{id}` 멱등, 200/202, 실패 412/422/503 | `remote.py:138-160` 클라이언트 `load_release`, `LOADED` 아니면 `get_release_state`로 폴링, `FAILED`면 거부, 503 계열은 `retryable` | 일치 |
| `GET /v1/releases/{id}` 상태 ABSENT/LOADING/LOADED/FAILED | `remote.py` 폴링 | 일치 |
| `extract` JSON, 503 `OUTPUT_INVALID`면 같은 요청 1회 재시도, 그래도면 NOT_COMMITTED·세션 불변 | `graph.py:492-498`(`is_output_invalid`, `MAX_EXTRACT_CALLS=2`) | 일치 |
| `respond` text/plain, `X-Openit-Outcome` | `RuntimeResult.is_fallback`을 `outcome_fallback` 통계로 셈(`remote.py:112`), `graph.py:757` `response_outcome=res.outcome` | 일치 |
| 적재되지 않은 릴리스 404 → `PUT`으로 적재 요청 | `remote.py:73-83` `_call_once` | 일치 |
| 호출 타임아웃 서비스 기본 4초 | `settings.py:115` 4.0 | 일치 |
| 서버가 번들을 스스로 읽는다 | 서비스도 같은 번들을 내려받음 | 계약 공백(SV-16) |
| 연결 실패 1회 재시도, 읽기 타임아웃은 재시도 없음 | `remote.py:97-110` | 계약은 "서비스 내부 동작이라 계약이 아니다"라고 한다(HTTP.md I9 마지막 줄) |

### F-2. 릴리스 읽기 I4 (INTERFACES.md I4, HTTP.md I4)
| 계약 | 구현 | 판정 |
|---|---|---|
| 활성 포인터 폴링, `pointer_version` 같으면 무동작 | `release/loader.py:79-83` | 일치 |
| 새 세션부터 새 릴리스 | `graph.py:433-444` (`found`면 `release_id_of`, 새 세션만 `releases.active()`) | 일치 |
| `runtime_contract_version` 미지원이면 적재 거부, 이전 유지 | `loader.py:203-207` | 일치 |
| `turn_engine_version`·`model_runtime_version` MAJOR.MINOR 불일치 거부, PATCH 경고 | `loader.py:240-253` | 일치 |
| `load_bundle`이 해시 검증, 비PASS는 FAILED_PRECONDITION | `loader.py:185` + 재검증(SV-17) | 일치(이중 확인) |
| 거부 이유를 `/readyz`에 | `LoaderStatus.rejections` | 일치 |

### F-3. Trace 전송 I6 (INTERFACES.md I6, CONVENTIONS 9·4)
| 계약 | 구현 | 판정 |
|---|---|---|
| `POST /v1/traces`, 키 `(session_id, turn_id)`, ACCEPTED/DUPLICATE | `trace/exporter.py:149-153` 둘 다 전달 완료 | 일치 |
| 턴 처리와 분리, 영속 버퍼 | `OutboxTraceSink.submit`→`outbox.enqueue`(턴), 전송은 스레드 | 일치 |
| "`UNAVAILABLE`일 때만 재시도" (I6, CONVENTIONS:43) | `exporter.py:123`, `exporter.py:138` `INTEGRITY_ERROR`와 일반 예외도 최대 20회 재시도 | **확장**(SV-18) |
| 비재시도 오류는 재시도하지 않음 | `mark_dead` | 일치 |
| 봉투 필드 `pipeline_schema_version`, `release_id`, `runtime_contract_version`, `session_id`, `turn_id`, `sent_at`, `trace` | `exporter.py:~48-52` | 일치(Id 변환은 CONVENTIONS 10절) |

### F-4. 공개 API I10 (PUBLIC_API.md ↔ api.py 한 줄씩)
| 계약 경로·항목 | 구현 | 판정 |
|---|---|---|
| 원칙: 세션 ID 1~64자 `[A-Za-z0-9_-]` | `api.py:50` `SESSION_ID` | 일치 |
| 오류 400 INVALID_RECORD / 401 / 403 / 404 / 409 / 429 / 503, `Retry-After`는 429·503 | `api.py:48,63-66`, `api.py:137-146` | 일치 |
| 401 토큰 없음·틀림, 403 권한 없음 | `api.py:~175-183` | 일치 |
| `POST /v1/sessions` 시작, `subject_ref` 선택 | `_start` `api.py:345-370`, `SUBJECT_REF` | 일치 |
| `POST /v1/sessions/{id}/turns` + `turn_id` 필수, 봉투 분리 | `_turn` `api.py:372-388`, `_envelope` | 일치 |
| `POST /v1/sessions/{id}/end` | `_end` `api.py:390-409` (끝난 세션 종료는 200 null) | 일치(계약은 이 경우를 말하지 않음, 합리적) |
| `GET /v1/sessions/{id}/state` + `resume_text`, `pause_reason`(PAUSED_RISK만) | `_state` `api.py:445-466` | 일치 |
| `GET /healthz`, `/readyz` 인증 없음 | `service.py` 핸들러 | 일치(본문 읽지 않음, 코드만 확인) |
| 직원: `GET /v1/staff/alerts`(status, limit 기본 50 최대 200, cursor) | `api.py:554-570` | 일치 |
| `GET /v1/staff/alerts/stream` SSE, `alert`·`resolved`·`heartbeat`·`resync`, `Last-Event-ID` | `service.py:405-430`, `api.py:749-757` | 일치(코드 전부를 줄 단위로 읽지는 않음, 시험으로 확인) |
| `GET /v1/staff/push-config`, `POST/DELETE /v1/staff/push-subscriptions`(201, 멱등) | `api.py:713-747` | 일치. 한도 초과 409(`api.py:731`)는 계약에 없는 추가 |
| `POST /v1/sessions/{id}/intervention`, `X-Openit-User` 없으면 401, 멈추지 않은 세션 409 | `_intervention` `api.py:411-443` | 일치 |
| `GET /v1/staff/sessions/{id}`(접근 기록, 기록 실패 시 데이터 불제공 503) | `_staff_session` `api.py:594-629`, `_NO_AUDIT` | 일치 |
| `GET /v1/results/{id}`, `GET /v1/results`(subject_ref, status, updated_after, limit, cursor), 접근 기록 | `api.py:631-696` | 일치 |
| 위험 신호 `POST /v1/sessions/{id}/risk-signals` 201/200 멱등, 404/409/400 | `_risk_signal` `api.py:698-711` | 일치 |
| 직원 호출 재알림·실패 기록, 429 INTERVAL(Retry-After)/RETRY_LIMIT(없음), 409 NO_OPEN_ALERT | `api.py:477-523` | 일치 |
| 키오스크 기기 토큰, 다른 기기 404 | `_device_denied` `api.py:236-248` | 일치 |
| `turn_id` 처리 중 409 `TURN_IN_PROGRESS` + `Retry-After` | `_in_progress` `api.py:333-337` | 일치 |

계약에 없는 응답(추가이며 위반 아님): 본문 64KB 초과는 400(`api.py:177`, 계약에 413 없음), SSE 연결 상한 초과 503(`service.py:410`), 구독 한도 409(`api.py:731`), 해당 역할 토큰이 설정되지 않은 결과 조회·위험 신호 경로는 404(`api.py:168-169`).

계약 문서 쪽의 낡음(헤드 문서): PUBLIC_API.md의 "VAPID 서버 키는 환경 변수로만 받는다"는 워크스페이스 규칙 D84(비밀은 `NAME_FILE`)와 다르다. 이 영역은 파일 우선으로 구현했다(boundary.md:27).

[미확인] `FrontendInput`, `SessionResult`, `StaffSessionView` 응답 JSON을 스키마로 일일이 대조하는 작업은 하지 않았다(시험이 `HttpPublicApi`로 검증한다고 문서가 말함).

## G. R7 (믿고 쓰기)

### SV-02 [R7, P1] A 출력을 `validate_extraction` + `check_slots` + `check_values`로 세 겹 재검증하고, 위반이면 모델을 다시 부른다
- 위치: `graph.py:512-531`, `extraction.py:92-172`, `extraction.py:31-36`, `docs/adr/ADR-0017.md`
- 인용: `viol = list(validate_extraction(inp, out, self.o.flow.table)); viol += extraction.check_slots(out, ...); viol += extraction.check_values(out, doc, ctx.bank, self.o.flow)` / `if st["extract_calls"] < MAX_EXTRACT_CALLS: ... return {"extract_next": "extract", ...}`
- 왜 위반: R7은 컴포넌트(LoRA-A, model-runtime의 스키마 검증 포함)가 내보낸 값을 믿고 쓰라고 한다. 이 영역은 (1) turn-engine이 이미 하는 의미 검증(`targets`·`next_item`·`save`)을 호출하고, (2) 그 뒤에 서비스가 같은 종류의 규칙을 다시 구현한다: 현재 문항 밖 저장 금지(`OUT_OF_CURRENT_ITEM`, 문항 계열 `_family`, 구간 `span`), 값 형식·크기(`_MAX_NUM`, `_MAX_STR`), 선택지·단위·예아니오 타입, 저장된 값과 종류 일치. 근거는 ADR-0017 "모델 출력은 신뢰하지 않는다"(심층 방어)로 R7과 정반대다. (3) 위반이 나오면 같은 요청을 한 번 더 부르고(D75) 그래도 틀리면 턴을 `NOT_COMMITTED`로 만든다. `Violation` 클래스도 turn-engine의 것을 그대로 본뜬 사본이다(`extraction.py:31-36`).
- 규칙에 의한 구분: 입력이 영역 바깥 신뢰 불가 입력(환자 발화)에 대한 검사는 해당하지 않는다. 여기는 컴포넌트 출력이다. 503 `OUTPUT_INVALID` 재시도(`graph.py:492-498`)는 값이 나오지 않은 호출의 재시도라 R7과 충돌하지 않는다고 본다 [판단].
- 제거했을 때 영향 [판단, 코드를 읽은 결과]:
  - 잃는 것: (a) 모델 의미 오류(NEXT_ITEM_MISMATCH 등)가 그대로 `plan_writes`로 간다. `plan_writes`는 `SKIP_REASON[output["reason"]]`(`extraction.py:~205`)처럼 검증을 전제한 인덱싱이 있어 `KeyError`/`StoreEffectError`가 날 수 있다. (b) `check_slots`는 문항 세트에 없는 칸 저장을 막는다. 없으면 `effects.apply_writes`가 `StoreEffectError`를 낸다(`store/effects.py`)[판단]. (c) 값 타당성(TE-1: 틀린 값이 기록에 들어감)은 서비스가 막는 유일한 층이다.
  - 같이 바뀌는 것: `validation_ok`, `validation_violations`, `extract_status=VIOLATION`(`TurnTrace` 키, design.md 19.1), `FailedStep.step=VALIDATE`, `/readyz`의 위반 통계(`Orchestrator.count_violations`), 로그, 시험(`test_extraction.py`, `test_orchestrator.py`의 위반 시나리오 외), `docs/boundary.md` 5절 "값 검증(TE-1)".
  - 계약 쪽: INTERFACES.md I8과 I9 "서비스가 지키는 것"이 이 재시도를 의무로 적고 있어(헤드 문서), 제거는 변경 요청과 turn-engine 영역의 협의가 먼저다. 제거하지 않으면 R7과 계약이 충돌한 채로 남는다.
- 최소 수정 방향: R7을 따른다면 (1) 검증과 재시도를 model-runtime/turn-engine 쪽으로 올려 서비스는 통과한 출력만 받게 하거나, (2) 이 영역에서 `n_validate_extraction`과 `extract_next`를 삭제한다. (2)는 헤드가 I8/I9를 고친 뒤에만 한다.

### SV-03 [R7, P1] 쓰기 확정 단계의 저장소 검사가 위 검증과 같은 규칙을 한 번 더 한다
- 위치: `extraction.py:92-103`(`check_slots`), `store/effects.py`(`apply_writes`의 `StoreEffectError`), `graph.py:668`
- 인용: `have = {effects.slot_key(r["slot"]) for r in records}` ... `Violation("UNKNOWN_SLOT", ...)`
- 왜 위반: "저장할 칸이 문항 세트에 있는가"를 `check_slots`(검증 단계)와 Session Store 효과 함수(쓰기 단계)가 각각 판정한다. 두 컴포넌트가 같은 규칙을 이중으로 구현한다. 어느 쪽이 정본인지 코드에 없다.
- 최소 수정: 저장소가 거부하고 그 결과(`StoreEffectError`)를 노드가 신호로 바꾸게 하거나, 한쪽만 둔다.

### SV-17 [R7, P2] 릴리스 로더가 I4 출력(번들)을 한 번 더 검증한다
- 위치: `release/loader.py:200-209`(`ReleaseManifest` 스키마·`content_hash`), `loader.py:211-223`(`get_decision`으로 PASS 재확인), `loader.py:225-239`(`QuestionBank` 스키마·해시·고정 문장)
- 인용: `if canonical.content_hash(manifest) != manifest["content_hash"]: raise LoadRejected("INTEGRITY_ERROR", ...)` / `if dec.get("result") != "PASS": raise LoadRejected("NO_APPROVAL", ...)`
- 왜: INTERFACES.md I4는 `load_bundle`이 "모든 해시를 검증한다 ... 통과(PASS)하지 않은 릴리스는 FAILED_PRECONDITION"이라고 이미 보증한다. 로더는 같은 해시·승인 판정을 다시 한다(release-gate의 보증을 믿지 않음). 근거는 brief §4("게이트가 승인한 묶음만 적재") — 요구는 "적재 거부" 결과이고 검증을 자기 손으로 하라는 말은 아니다. `REQUIRED_FIXED_SENTENCES` 검사(`loader.py:~238`)는 서비스 자신의 요구(고정 문장 4개)라 R7 대상이 아니다.
- 최소 수정: I4가 보증하는 해시·PASS 재검증을 제거하고 `load_bundle`의 오류(`FAILED_PRECONDITION`, `INTEGRITY_ERROR`)를 그대로 거부 사유로 쓴다. 단 헤드가 "방어 심층"을 R7의 예외로 두는지 먼저 확인할 일.

### SV-18 [R7·F, P3] Trace 송신 전 로컬 재검증, 계약보다 넓은 재시도
- 위치: `trace/exporter.py:81-97`(`_local_errors`), `exporter.py:118-141`
- 인용: `errs = list(self._env_validator.iter_errors(env))` / `if e.retryable or e.code == "INTEGRITY_ERROR":` / `err = f"UNEXPECTED: {type(e).__name__}"`
- 왜: (1) 이 영역이 만든 `TurnTrace`를 같은 영역의 전송기가 스키마로 다시 검증한다(수집이 `INVALID_RECORD`로 어차피 검증한다, I6). 자기 출력이므로 R7의 "이웃 컴포넌트가 믿고 쓴다"에 해당하고, 위반 시 `LOCAL_INVALID`로 DEAD 처리해 잃는 Trace가 생긴다. (2) INTERFACES.md I6·CONVENTIONS.md:43은 "UNAVAILABLE일 때만 재시도"인데 `INTEGRITY_ERROR`와 일반 예외도 20회까지 재시도한다. 의도된 보강(boundary.md:58 A6 F6)이고 멱등이라 안전하지만 계약 문구와는 다르다.
- 최소 수정: (1) 검증은 시험(계약 시험)으로 옮기고 운영 경로에서 뺀다 [판단]. (2) 계약에 "깨진 응답은 재시도 가능"을 명시하도록 변경 요청.

### G 보조: 중복이 아닌 것으로 확인한 곳
- B 입력 조립(`extraction.build_response_request`)은 A 출력에 대한 재검증이 아니다. `n_respond`는 `respond` 오류를 신호로만 바꾼다(`graph.py:746-756`), 재시도 없음.
- 추론 클라이언트의 연결 실패 1회 재시도(`remote.py:97-110`)는 호출 실패 재시도이며 값 재검증이 아니다.
- 입력 길이 상한(`graph.py:483-487`, 2400자 truncate)은 model-runtime의 입력 400 거절(2500자)을 미리 막는 이웃 한도 사본이다(ADR-0012). 이웃 한도 값을 복사했다는 점은 R1 성격의 P3이나 별도 ID는 두지 않는다 [판단].

## P3 기타 (문서 정리 수준)

### SV-19 [R1, P3] 문서에 이웃 사실의 사본과 낡은 서술
- 위치: `docs/design.md:11`(공개 API "Bearer 토큰(환경 변수의 이름으로 지정, `SURVEY_API_TOKEN_ENV`)"), `docs/design.md:182`("VAPID 비밀키는 환경 변수 `SURVEY_VAPID_PRIVATE_KEY` ... 로만 받는다") vs `docs/boundary.md:27`(`SURVEY_VAPID_PRIVATE_KEY_FILE`, D84), `docs/design.md:140`("공개 API(I10): O06 대기")와 §11, `docs/brief.md` §2("O06 상태는 미확인"), `docs/boundary.md:87`(deploy 비밀 이름 사본)
- 왜: 한 사실이 문서마다 다른 시점으로 남았다(공통 규칙의 "복사하지 않고 링크"). 코드는 공개 API를 구현했는데 design.md 13·11절은 대기라 쓴다.
- 최소 수정: 낡은 절을 지우고 한 곳만 남긴다.

### SV-20 [R5, P3] 세션 문서의 정지 쓰기 모양이 세 곳에 복제돼 있다 (SV-04의 부산물)
- 위치: `graph.py:346`, `graph.py:554-556`, `orchestrator.py:197-205`
- 인용: `pu = {"current_question_id": ..., "open_slots": ..., "skipped_add": [], "skipped_remove": [], "session_status": "PAUSED_RISK", "pause_history_add": [...]}`
- 왜: `progress_record_update` 딕셔너리를 생성하는 코드가 키 이름을 각자 쓴다(`TurnWriteSet`은 스키마가 있으나 생성 함수가 없다).
- 최소 수정: `effects.py`에 `pause_update(...)` 같은 한 함수.

## 요청한 단서별 답

1. **컴포넌트·할당 I/O·TurnState**: A절 표. `TurnState`는 최상위 키가 TypedDict로 기계가 읽는 형태이지만(R5 부분 충족), 노드별 입출력 선언이 없고 노드 사이 값 대부분이 `dict`다(SV-10). 노드는 `self.o`로 모든 자원에 접근한다(SV-06). 상태 키로 노드끼리 제어 흐름을 정한다(SV-01).
2. **turn_engine 사용**: 사용 함수는 `validate_extraction`, `ContractViolation`(graph.py:27), `QuestionFlowTable`, `SkipRule`(flow.py:13), `ENGINE_VERSION`(service.py:111)뿐이다. `normalize/judge/plan`은 쓰지 않는다(boundary.md:29). R7과 어긋나는 곳과 제거 시 영향은 SV-02·SV-03.
3. **I9·I4·I6 소비**: F-1~F-3 표(각각 한 줄씩). 어긋남은 I6 재시도 범위(SV-18)와 번들 이중 읽기(SV-16, 계약 공백)뿐.
4. **I10 대조**: F-4 표. 경로·필드·상태 코드·오류에서 계약과 어긋난 곳은 찾지 못했다. 계약에 없는 추가 응답 4건 기록.
5. **문진 스키마 R5/R6 구분**: SV-14 표.
6. **이웃 구조에 기대는 곳 / 계약 밖 형식**: D절(SV-11~13), SV-15(`QuestionFlow`→`QuestionFlowTable` 변환 포함).

## 점검하지 못한 것

- 실행하지 않음: 전체 시험(PG 필요한 시험 포함), `scripts/smoke.sh`(이미지 빌드), `smoke_compose.sh`, 계약 저장소 자체의 `tests/`·`verify.py`.
- 읽지 않았거나 일부만 읽음: `alerts.py` 본문(클래스 목록·`AlertStaffConsole`·ContextVar 사용부만), `store/*.py` 본문 대부분(`effects.py`는 사용부만), `service.py` 본문(import·SSE 상한·버전 읽기 부분만, 576줄 중 일부), `response_tables.py`(내용 전체 미독, 헤더와 문서만), `risk_rules.py`(클래스 개요만), `settings.py`(일부), `retention.py`, `db.py`, `pg.py`, `netguard.py`, `trace/outbox.py`, `cli.py`, `schema/schemas/*.json` 본문(`QuestionFlow.json` 일부만), `docs/adr/` 22개 중 README와 ADR-0017만, `docs/change-requests/` 본문, `docs/runbook.md`, `tests/` 본문(목록과 일부 실행만).
- F-4에서 `FrontendInput`, `SessionResult`, `StaffSessionView`, `SessionState`의 필드를 스키마와 하나씩 대조하지 않았다. SSE 핸들러(`service.py:~400-470`)와 `/healthz`·`/readyz` 핸들러는 줄 단위로 읽지 않았다.
- SV-02의 "제거 시 영향" 중 `plan_writes`의 KeyError, `effects.apply_writes` 예외는 코드 읽기로 낸 추정이며 제거를 실제로 시도하지 않았다.
- SV-14의 "현재 비호환 변경이 해소됐는가"는 문서(design.md:222)만으로 판단했고 head의 DECISIONS와 대조하지 않았다.
- 비밀 값, 환경 변수, `secrets/`는 읽지 않았다.
