# CR-001 문진 스키마 변경 후보 3건 (적용 안 함)

> **상태: 스키마 v2 후보 목록으로 보류. 세 건 모두 정수 버전 상승(1→2)을 부르므로 헤드가 모아 한 번에 올린다. 그때까지 적용하지 않는다(헤드 결정).**
>
> **[역사 기록]** 아래에 나오는 `write_record`, `normalize`, `judge`, `plan`(Planner)은 v1 turn-engine 함수로, turn-engine v0.4.0에서 제거됐다. 현재 구조는 `docs/design.md` 16절(D74)이다. 이 문서는 당시의 요청을 그대로 둔다.


- 요청자: survey-service (스키마 소유자, D21). 이번 구간에서는 `schema/`를 바꾸지 않았다.
- 계약 버전: 문진 스키마는 정수 버전이 정확히 일치해야 하고 모든 스키마가 `additionalProperties: false`다. 아래는 모두 **필드나 열거값을 더하는 변경**인데도 읽는 쪽이 이전 스키마로 검증하면 거부되므로 정수 버전을 올려야 할 가능성이 크다. 올릴지, 어떤 시점에 묶을지는 헤드가 정한다. 세 건은 한 번에 묶어 한 번만 올리는 것을 제안한다.
- 근거는 turn-engine CR-005·CR-007과 골든 K4 예시이다(해당 CR 문서는 읽지 않았고 `tests/golden_known.json`/CR-009의 요약과 요청 문장만 근거로 썼다. 확인되지 않은 부분은 아래에 "확인 필요"로 표시).

## A. UtteranceJudgment에 고정 문장 정보 추가 (골든 K4)

- 문제: 무관한 말이 답과 함께 왔을 때 고정 문장 `OOS_NOTICE`를 문장 재료에 더해야 하는데, Planner 입력에는 무관 항목이 있었다는 정보가 없어 도출할 수 없다(CR-009의 K4/t1/DialoguePlannerInput).
- 제안 1(권장): `UtteranceJudgment.out_of_scope_present: boolean`(선택, 기본 false). Judge가 사실만 낸다. 문장(고정 문장 키 선택)은 Planner가 정한다(원칙 3·7과 맞다).
- 제안 2: `UtteranceJudgment.fixed_sentence_keys: [FixedSentenceKey]`. Judge가 문장 키까지 정하므로 판단이 한 컴포넌트 앞으로 당겨진다. 권장하지 않는다.
- 영향 영역: turn-engine(Judge 출력, Planner 입력 해석), model-lab·synthetic-data(Judge/Planner 정답 데이터가 있다면 재생성), release-gate(`runtime_contract_version`), ops-feedback(TurnTrace 검증 버전), survey-service(스키마 패키지, 트레이스). question-authoring은 영향 없음.

## B. StaffAlertReason에 도움 요청 사유 추가

- 문제: 골든 F3("어브브 그 저")처럼 사용자가 도움을 요청한 것이 아니라 인식 실패가 이어져 직원을 부르는 경우 `StaffAlert{kind: CALL_STAFF, reasons: []}`로 사유가 비어 있다. 직원 화면에서 왜 불렸는지 알 수 없다. 현재 `StaffAlertReason`은 `RISK_SIGNAL`, `RISK_RULE`, `USER_CALLED_STAFF`뿐이다.
- 제안: 사유 하나를 더한다. 예: `{kind: "HELP_REQUEST", cause: "UNRECOGNIZED_STREAK" | "ASK_LIMIT" | "CONFIRM_LIMIT"}`(열거값은 Planner가 이미 가진 한도 개념에서 가져온 것이고, 정확한 목록은 turn-engine이 정한다). 확인 필요: 규칙 문서(`dialogue_planner.md`)는 이 영역에 없어 읽지 못했다. "도움 요청 사유"가 사용자의 요청(USER_CALLED_STAFF)과 구분되는지 헤드가 확인해 달라.
- 영향 영역: turn-engine(Planner 출력), 직원 콘솔(StaffConsoleInput을 받는 쪽, 현재 팀 외부), survey-service(전달·트레이스), ops-feedback(TurnTrace 검증).

## C. RecordWriterInput에 pending_id_seed 추가

- 문제: `write_record`가 `pnd-`로 시작하는 결정적 확인 대기 ID를 만든다(turn-engine boundary §2). 입력만으로 ID가 정해지므로, 같은 슬롯에 확인 대기를 만들고 버리고 다시 만드는 흐름에서 ID가 되풀이될 수 있고 서로 다른 턴을 구분할 수 없다. 확인 대기 ID로 응답 지시(`target_pending_id`)와 판정을 연결하므로, 되풀이된 ID는 오래된 응답 지시와 새 확인 대기를 헷갈리게 한다.
- 제안: `RecordWriterInput.pending_id_seed: string`(선택). Orchestrator가 `turn_id`를 넣고 `write_record`는 ID를 `pnd-<해시(seed, 슬롯)>`로 만든다. 같은 입력에 같은 출력이라는 순수성은 유지된다. 확인 필요: turn-engine이 이미 ID 충돌을 다른 방식으로 막는지는 이 영역에서 확인하지 못했다(코드를 읽지 않는다).
- 영향 영역: turn-engine(writer), synthetic-data·model-lab(정답 데이터 재생성이 필요하면), survey-service(입력 조립), ops-feedback(TurnTrace 검증).

## 이 영역이 정리한 순서 제안

한 번의 정수 버전 상승(v2)에 A·B·C를 묶고, 그동안 스키마 패키지는 v1을 낸다. 패키지는 `schema/v2/`를 추가하면 같은 wheel에서 v1·v2를 함께 내므로(여러 정수 버전을 동시에 지원하는 소비자용) 이행 기간을 둘 수 있다.

## D. 조건 규칙을 oneOf 가지로 표현 (추가 후보, 헤드 지시 D48)

- 문제: vLLM의 구조화 출력(제약 디코딩)이 `allOf` / `if` / `then` / `not`을 무시한다(D48). 스키마가 조건으로만 표현한 규칙(예: `Item`에서 `intent == ANSWERED_INTENT`이면 `target`·`value`가 null이 아니어야 함, `SlotDef`의 `choice_catalog`가 있으면 `choices`가 비어야 함, `SessionLoadResult`·`SessionCommitResult`·`StoreDecision`의 상태별 조건)이 제약 단계에서 걸러지지 않고 사후 검증과 재시도에 맡겨진다.
- 제안: 조건 규칙을 `oneOf`(또는 `anyOf`) 가지로 다시 쓴다. 가지마다 판별 필드를 `const`로 고정한다(예: `intent`가 `ANSWERED_INTENT`인 가지는 `target`·`value`를 null 불가로, 나머지 가지는 허용). 의미는 같고 표현만 바뀐다.
- 영향 영역: model-runtime(제약 디코딩이 받는 스키마, 완화 정책 `relax`와 사후 검증), model-lab·synthetic-data(출력 형식 정답 데이터), turn-engine(입력·출력 검증은 같은 의미라 영향 작음), release-gate(`runtime_contract_version`), ops-feedback(TurnTrace 검증), survey-service(스키마 패키지). 
- 계약 버전: 허용 집합이 같은 표현 변경이지만 스키마 파일이 바뀌므로 정수 버전 상승(v2)에 함께 묶는 것을 제안한다. 허용 집합이 같음을 확인하는 시험(기존 예시·골든 표본이 같은 결과로 통과/거부)을 반영 전에 돌려야 한다. 확인하지 못한 것: 각 조건 규칙 전체 목록(여기서는 스키마에서 눈에 띈 예만 적었다).
