# 계약의 근거가 되는 업무 요구

이 문서들은 각 필드를 사용해야 하는 상황과 담당 컴포넌트의 기대 책임을 설명한다. Schema 프로젝트의 실행 코드나 자동 검사 결과가 아니다. 예를 들어 저장 효과나 질문 순서의 설명이 있다고 해서 검증기가 그 동작을 실행하는 것은 아니다.

문서 목록은 `manifest.yaml`에 있다. `principles.md`는 설계 원칙, `mapping.md`는 역할과 계약의 대응, `timing.md`는 조회 시점, 컴포넌트별 문서는 업무 요구, `verification.md`는 이 프로젝트의 검사 범위를 설명한다. `trace_contract.md`는 기록 형식을 설명한다.

값의 실제 구조는 `../contracts/`의 Schema·필드 설명이 정한다. 추가 자료형 제안은 `../contracts/proposals.yaml`에 있으며 현행 계약과 구분한다. 예시 발화·문항 값은 설명용이고, 실제 선택지·범위·기준은 QuestionBank 데이터가 정한다.
