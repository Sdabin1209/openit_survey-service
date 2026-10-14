# CR-004 컴포넌트 입력의 저장소 조회 범위 확인

- 상태: 확인 요청. 영향은 survey-service 안의 해석이다.
- 배경: principles 4는 "저장소에서 일부만 가져오면 가져오는 범위를 적고 범위는 판단 없는 고르기만 허용"한다고 한다. 컴포넌트 입력 필드별 출처 표는 원본 `contracts/`에 있고 이 워크스페이스에는 없다. 그래서 이 영역이 스키마 필드 이름과 골든에서 범위를 정했다(`docs/design.md` 3.1의 표).
- 확인이 필요한 것 (가장 영향이 큰 순):
  1. `AnswerNormalizerInput.related_questions`: 항목 대상 문항만인지, 선행 조건 문항과 직전에 기다리던 문항까지 포함하는지.
  2. `AnswerJudgeInput.pendings`: 세션 전체의 확인 대기인지, 관련 문항의 것만인지.
  3. `AnswerNormalizerInput.turn_index`: 대화 로그 항목 수(처리 안 함 턴 포함)가 맞는지, 발화 턴만 세는지.
  4. `UtteranceParserInput.awaiting_questions`: 직전 응답 지시의 `awaiting` 대상 문항인지, `targets`인지.
- 현재 구현은 표의 해석을 따르고, 원본이 다르면 한 곳(`survey_service/assembly.py`)만 바꾸면 된다.
