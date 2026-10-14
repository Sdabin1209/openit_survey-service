## 1. 역할과 계약의 대응

자연어 사실 추출은 Utterance Parser(LoRA-A), 표준화·정형 검사는 Answer Normalizer, 상태 판정은 Answer Judge, 저장할 변경은 Record Writer, 다음 행동과 문장 재료는 Dialogue Planner가 담당한다. Response Generator(LoRA-B)는 정해진 재료를 자연어로 표현하고 Orchestrator(LangGraph)는 이 경계들을 연결한다.

정규화·상태 판정·저장 판단·계획 결정을 별도 계약으로 구분한다. 모델 출력에 저장 여부나 다음 질문의 결정을 맡기지 않도록 책임을 분리한다. 상세 구조와 입력·출력 계약 목록은 [구조와 책임](../../docs/architecture.md)과 [components.yaml](../contracts/components.yaml)에 있다.

업무 요구에서 정상·누락·애매·충돌·정정·모름·거절·무관·완료는 시나리오 상황이다. 모든 상황이 하나의 상태 enum에 모이는 것은 아니다. 항목 상태는 JudgmentStatus, 발화 전체 판정은 UtteranceJudgment, 계획의 응답 행동은 Action, 실행 흐름은 Flow, 세션 진행은 SessionStatus로 구분한다.

각 역할의 입출력 구조와 생산·소비 책임은 이 저장소의 계약 원본에서 확인한다.
