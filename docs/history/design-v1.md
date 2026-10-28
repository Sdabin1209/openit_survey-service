# survey-service 설계서: v1 구조 시절 절(이력)

> 이력 문서다. v1(파서-정규화기-판정기-기록기-Plan-생성기) 구조의 서술이며 현행이 아니다. 현행 구조는 `docs/design.md` 16~19절이 정한다. 이 파일은 현행 문서에서 참조하지 않는다.

## 3. 한 턴의 흐름

입력은 `FrontendMessage`(SESSION_START, UTTERANCE, SESSION_END) 또는 `InterventionDecision`(RESUME, END) 중 하나와 호출자가 주는 `turn_id`다. [제안] `turn_id`는 호출자(나중에 공개 API)가 정하고, 같은 `(session_id, turn_id)`를 다시 받으면 저장한 결과를 그대로 돌려준다(이중 저장 없음, brief §4). 같은 턴 ID에 다른 입력이 오면 `TurnIdConflict`.

1. **세션별 직렬화** [확인: preconditions 0-1.1]: 세션마다 락. 앞 턴이 끝나기 전의 입력은 기다린다. 여러 프로세스로 늘릴 때를 위해 커밋은 `expected_revision`으로 낙관적 검사를 한다(`REVISION_CONFLICT`면 쓰기를 반영하지 않고 턴 실패).
2. **턴 맨 앞 조회**: 저장소에서 세션을 찾는다(`SessionLoadRequest/Result`). `OrchestratorInput`을 만든다. `latest_question_bank_version`은 현재 활성 릴리스의 문항 세트 버전이다.
3. **처리 표(9-1)**:

| 입력 \ 세션 | 없음 | 진행 중 | 일시 중지 | 종료 |
|---|---|---|---|---|
| SESSION_START | 세션 생성 후 Plan(SESSION_START) | Plan(SESSION_START) | 처리 안 함(로그 남김) | 처리 안 함(로그 없음) |
| UTTERANCE | 세션 생성 후 전체 흐름 | 전체 흐름 | 처리 안 함(로그 남김) | 처리 안 함(로그 없음) |
| RESUME | 처리 안 함 | 처리 안 함 | Plan(RESUME) | 처리 안 함 |
| END | 처리 안 함 | Plan(INTERVENTION_END) | Plan(INTERVENTION_END) | 처리 안 함 |
| SESSION_END | 처리 안 함 | Plan(END_LEFT) | Plan(END_LEFT) | 처리 안 함 |

   "처리 안 함"은 어떤 컴포넌트도 부르지 않고 응답이 없다. 개입 결정이면 `intervention_result = IGNORED`. 로그 규칙은 9-1의 3항.
4. **세션 생성**: 초기 `SessionStore`(모든 슬롯 NONE, 진행 기록·Plan 기록 초기값)를 만들고 현재 활성 릴리스에 묶는다(새 세션부터 새 릴리스, O13). 실패하면 그 턴은 로그 없이 `NOT_COMMITTED` 신호(J8a).
5. **전체 흐름(발화)** [D74 이후는 16절]: extract(LoRA-A 한 번) → validate_extraction(turn-engine) → plan_writes. 어느 단계든 실패면 `BEFORE_COMMIT` 실패로 멈추고 이후 단계를 하지 않는다. 시작·재개·종료·이탈 턴은 A를 부르지 않고 `plan_other`만 한다.
6. **쓰기 확정**: 저장 결정 + 진행 기록 갱신 + Plan 기록 갱신을 한 번에 커밋한다(`SessionCommitRequest.writes`). 실패하면 아무것도 반영하지 않는다. 실패 시 확정 뒤의 일은 하나도 하지 않는다(preconditions 0-1.3, J8f).
7. **확정 뒤의 일(`AFTER_COMMIT`)** 은 서로의 성패와 무관하게 모두 시도한다: 직원 알림 전달(위험 입력원이 알림을 냈을 때, 19.4), 결과 전달(문진이 끝난 턴), 응답 생성(LoRA-B 또는 고정 문장), 프론트 전달. 직원 알림 전달은 지연 없이 하기 위해 응답 생성보다 앞에 한다.
8. **로그와 신호**: 대화 로그(`LogEntry`)는 턴 맨 끝에 따로 쓴다(실패한 턴도). 실패 단계가 하나라도 있으면 `TurnFailureSignal{write_committed, staff_alert}`를 프론트·직원 콘솔·출력에 싣는다. 개입 결정이면 `intervention_result`는 쓰기가 반영되면 APPLIED, 반영되지 않으면 FAILED, 처리 안 함이면 IGNORED.
9. **Trace**: 턴이 끝나면 `TurnTrace`를 조립해 `TraceEnvelope`(릴리스 ID, 계약 버전 포함)로 outbox에 넣는다(§6). 이 한 번의 로컬 쓰기 외에 턴 응답이 전송을 기다리지 않는다.

### 3.1 컴포넌트 입력 조립

필드마다 원천 계약·필드·읽는 시점(snapshot)·전달 방식은 `docs/rules/sources/bindings.yaml`(헤드 전달, 원본 `work/contracts/`)이 정한다 [확인]. `tests/test_bindings.py`가 조립한 입력을 이 표와 필드 단위로 대조한다: 필드 집합이 표와 같은지, turn_start와 post_store_write 시점, project/lookup/copy 방식, 판정기 `pendings`에 `value`·`risk_mark`가 없고 정규화기 `pendings`에 `risk_mark`가 없는 것 등. 처음 대조에서 다른 곳은 하나였다(판정기 `affected_followups`를 비어 있지 않은 문항만 냈는데 표는 `questions[]` 전부의 투영이다. 고쳤다). `AnswerNormalizerInput.awaiting`은 표가 "last_directive.awaiting"이라 하지만 계약이 `say_values`를 허용하지 않아 `{action, targets, target_pending_id}`로 투영한다.

표가 정하지 않고 `selection_owner`가 Orchestrator인 **선택 정책**은 이 영역이 정했다 [결정]:

| 입력 필드 | 표가 정한 것 | 이 영역의 선택 정책과 근거 |
|---|---|---|
| `UtteranceParserInput.awaiting_questions` | 직전 지시 `awaiting.targets`가 참조하는 문항 정의를 중복 없이 | 문항 `order` 순. 순서는 표에 없어, 같은 입력이 같은 출력을 내도록(결정성) 정렬한다 |
| `UtteranceParserInput.answered_slots` | 현재값이 `{answer}`인 행 | 반복 키 슬롯 포함 모든 행. 모름·거절·없음은 답이 아니다(9-2) |
| `AnswerNormalizerInput.related_questions` | 선택자 `item_target_questions`, `awaiting_slot_questions`, `direct_prerequisite_questions` | 선택자 세 개의 합집합을 문항 `order` 순으로. 직접 선행만(선행의 선행은 넣지 않는다) |
| `current_values`, `pendings` | "선택한 answer_records" | 관련 문항에 속한 모든 슬롯 행(반복 키 포함). 행 단위로 고르면 같은 문항의 일부 슬롯만 보여 누락 판정이 흔들린다 |
| `AnswerJudgeInput.pendings` | 확인 대기가 있는 선택 행 | 세션 전체의 확인 대기. 판정기가 확인 응답을 짝짓는 데 관련 문항 밖의 대기도 필요할 수 있어 줄이지 않는다 |
| `RecordWriterInput.target_records` | 판정 대상·`invalidated_questions`에 해당하는 슬롯 기록 | 대상이 문항 전체면 그 문항의 모든 슬롯(반복 키 포함) |
| `turn_index` (표: 선택 주체 Framework) | 호출 문맥의 순번, Trace의 `turn_index`와 같게 | 턴 시작 때 대화 로그 항목 수(처리 안 함 턴 포함). 정규화기 입력과 Trace에 같은 값 |

### 3.2 저장소 반영 효과

`survey_service.store.effects`가 9-2의 두 표를 그대로 구현한다. 판단 없이 효과만 적용한다. 반영할 수 없는 결정(없는 슬롯, 맞지 않는 확인 대기 ID)은 `StoreEffectError`로 커밋을 거부한다(쓰기 전체가 반영되지 않는다). 규칙 문서의 모호한 곳은 아래처럼 해석했다.

| 모호한 곳 | 해석 [제안] |
|---|---|
| 건너뜀 추가와 해제가 같이 올 때 | 해제를 먼저(규칙 명시). 확인 중단도 같은 순서로 |
| 확인 중단 추가가 이미 있는 슬롯 | 중복 없이 한 번만 |
| 이력에 쌓는 "이전 답" | 현재값이 값(answer)일 때만. 모름·거절·NONE은 쌓지 않는다. 이전 값에 `value_metadata`가 있으면 `{value, value_metadata}` 항목 |
| 첫 저장·새 revision의 `value_metadata` | 저장 결정에 있으면 슬롯 기록에 둔다. 없으면 슬롯 기록에서 지운다(이전 값의 정밀도를 새 값에 붙이지 않는다) |
| 확인 대기 받아들임 | 확인 대기 기록의 `value`, `risk_mark`, `value_metadata`가 현재값·위험 표시·메타데이터가 된다 |


## 10. 골든 예시와의 대조 [폐기: v1 컴포넌트 출력 재생이라 D74 구조에는 맞지 않는다. 골든 시험과 재생 도구를 지웠다]

방법은 `tests/golden_replay.py`, `tests/test_golden.py`, `tests/golden_known.json`에 있다. 결과: 226 케이스, 처리한 턴 234(형식·표현 예시 13턴은 스키마 검증만), 비교한 쌍 1220(OrchestratorInput, OrchestratorOutput, FrontendInput, 세션 저장소의 처리 시작·최종 상태, StaffConsoleInput, DispatchInput/Output) 중 1215 일치, 5 불일치(모두 (c) 예시 오류). 골든 표본 1704개는 모두 자기 계약 스키마를 통과한다. 요약: 골든에는 문항 세트와 모델·엔진 입력이 없고(오케스트레이터 계약만 있다) 로그 항목 안에 컴포넌트 **출력**이 있다. 그래서 컴포넌트 출력을 재생하는 엔진·모델 대역으로 Orchestrator 자체(입력 조립, 처리 표, 저장 반영, 진행 기록, 로그, 실패 처리, 직원 알림, 결과 전달)를 비교한다. 실제 turn-engine을 골든 입력으로 돌리는 비교는 문항 세트가 없어 못 한다(CR-002는 원본에 없어 해결할 수 없다고 닫혔다). 입력 조립은 골든이 아니라 bindings.yaml과 대조한다(3.1).


### 14.1 그래프 (노드와 조건부 간선)

```
                         +--> fail_no_session ------------------------------------------+
                         |    (조회 실패, 세션 정보 생성 실패, 활성 릴리스 없음)            |
 START -> load_session --+--> col_none ----+                                              |
          (OrchestratorInput  col_progress -+--(route 없음 = 처리 안 함)--> ignore --------+--> END
           조립)              col_paused ---+                                              |
                              col_ended ----+--(route 있음)--> bind_release --+            |
                                                                              |            |
        bind_release: 릴리스 결정, 세션 정보 생성 ---(실패)--> finalize --------+            |
              |  (발화: full)                  (plan만)                                      |
              v                                  |                                          |
   parse -> normalize -> judge -> write_record -> plan -> commit --(실패)--> finalize -------+
     |          |          |          |            |
     +----------+----------+----------+------------+--(단계 실패)--> finalize
                                                    commit --(확정)--> deliver_staff
                                                      deliver_staff --(flow=END)--> dispatch_result --> generate
                                                      deliver_staff --(그 밖)-------------------------> generate
                                                      generate -> finalize -> END
```

- **처리 표 9-1의 대응**: 열(세션 상태) = `load_session` 뒤의 조건부 간선(`col_none` / `col_progress` / `col_paused` / `col_ended`), 행(이벤트 종류) = 각 열 노드 안의 표 한 열(`Route`: 턴 종류, 세션 생성 여부, 전체 흐름 여부, 처리 안 함일 때 로그 여부). 열 노드 뒤의 간선이 `route 없음 -> ignore`, `route 있음 -> bind_release`다. "전체 흐름"은 `bind_release` 뒤의 간선(`parse` 또는 `plan`), "Plan만"은 `plan`으로 바로 간다.
- **단계별 실패**: `parse`~`commit` 노드는 예외를 잡아 `failed`에 `BEFORE_COMMIT/<단계>`를 더하고, 다음 간선이 `failed`가 비어 있지 않으면 `finalize`로 보낸다(규칙 4: 앞 단계로 돌아가지 않고, 쓰기 확정이 실패하면 확정 뒤의 일은 하지 않는다). 확정 뒤 노드(`deliver_staff`, `dispatch_result`, `generate`)는 서로의 성패와 무관하게 이어진다(`AFTER_COMMIT` 실패를 더할 뿐).
- **LangGraph가 분기하는 것**: 처리 표, 흐름 종료(`flow == END`일 때 결과 전달), 턴 실패, 그리고 규칙 문서 9-1 2항이 말하는 대로 판정 상태·행동 종류로는 분기하지 않는다.

### 14.2 상태 (직렬화 가능)

`TurnState`(TypedDict)의 값은 모두 JSON으로 직렬화되는 dict·list·문자열·수·bool·None이다. 모델 세션, 저장소, 엔진 같은 **자원은 상태에 두지 않고** `Orchestrator`가 가진다(릴리스 객체만 호출마다 `config["configurable"]["ctx"]`로 노드에 건넨다).

| 필드 | 뜻 |
|---|---|
| `session_id`, `turn_id`, `input_hash`, `message`, `decision`, `input_kind`, `in_kind`, `utterance` | 입력 |
| `loaded`, `found`, `status` | 턴 맨 앞 조회 결과 |
| `route` | 처리 표의 한 칸 `{turn_kind, create, full, keep_log}` |
| `release_id`, `state_doc`, `revision`, `created` | 릴리스 결정, 턴 시작 문서, 낙관적 revision, 세션 정보 생성 결과 |
| `parser_out`, `norm_out`, `judge_out`, `writer_out`, `records_after`, `plan_out` | 컴포넌트 출력과 저장 결정 반영 뒤 기록(timing 0-2) |
| `committed`, `commit_unknown`, `post_doc` | 쓰기 확정 결과와 확정 뒤 문서 |
| `failed`(더하기 reducer), `staff_status`, `staff_delivery`, `response_text`, `signal`, `intervention_result`, `dispatch_called`, `console`, `console_delivered`, `console_calls`(더하기) | 확정 뒤의 일과 실패 신호 |
| `trace`(병합 reducer) | TurnTrace 재료. 노드가 채운 칸만 병합 |
| `fatal`, `orchestrator_output`, `frontend_input`, `console_input`, `out_release_id`, `cacheable` | 종료 노드의 결과 |


### 14.5 구현 (survey_service/graph.py)

- 노드는 `TurnGraph`의 `n_<이름>` 메서드이고 이전 `_Turn` 기반 함수 본문을 옮긴 것이다(필드 순서·값·직원 콘솔 호출 순서 불변). 열 표는 `COLUMNS`, `route()`는 그 표를 읽는 얇은 함수다. 상태는 `TurnState`, 시작 상태는 `initial_state()`, 호출마다 건네는 자원(릴리스, 문항 세트 보조 객체)은 `TurnCtx`다. `Orchestrator.handle_turn`은 락, 검증, 멱등, 결과 저장, Trace outbox를 그래프 밖에서 한다.
- 회귀: 기존 시험 전체와 골든 대조(1220쌍 중 1215 일치, 5건 예시 오류)가 그대로 통과한다. `tests/test_graph.py`가 간선 구성, 되돌아가는 간선이 없음, 체크포인터 없음, 표와 `route()`의 일치, 모든 경로의 최종 상태가 JSON 직렬화 가능함, 턴 처리 중 네트워크 연결 없음, 추적 환경 변수가 꺼져 있음, 잠금 파일의 langgraph 고정과 설치본 일치를 확인한다.
- 의존: `langgraph` 1.2.15(잠금 파일에 정확히 고정, `pyproject.toml`은 `>=1.2,<1.3`). langchain-core, pydantic, langsmith 등 43개 패키지가 잠금에 들어온다(이전 6개). 이미지 라벨 `io.openit.langgraph`(CR-011), 이미지 환경에 `LANGSMITH_TRACING=false`, `LANGCHAIN_TRACING_V2=false`.

