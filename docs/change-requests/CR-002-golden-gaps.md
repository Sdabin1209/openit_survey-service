# CR-002 골든 예시 보강 요청

- 대상: `tests/golden/` (오케스트레이터·프론트·세션 저장·로그·직원 콘솔·결과 전달·Trace 계약 예시, 226 케이스)
- 상태: 요청. 이 영역의 대조 시험은 아래 한계 안에서 동작한다(`tests/test_golden.py`).

## 빠진 것

1. **문항 세트(QuestionBank)**: 골든 어디에도 없다. 그래서 실제 turn-engine에 골든 입력을 넣어 같은 결과가 나오는지 이 영역에서는 확인할 수 없다. 초기 응답 기록(모든 슬롯의 목록과 순서)에서 문항·슬롯 구성만 복원해 쓴다.
2. **모델 입출력**: `UtteranceParserInput`, `ResponseGeneratorInput`, `GeneratorOutput`이 없다. 파서 출력은 로그 항목의 `parser_output`에서, 생성 문장은 `FrontendInput.response_text`에서 얻는다.
3. **컴포넌트 입력**: Normalizer·Judge·Writer·Planner의 입력이 없다. Orchestrator가 저장소에서 어떤 범위를 골라 조립하는지(timing, principles 4)를 비교할 수 없다(CR-004).
4. **TurnTrace**: Trace 계약 예시가 없다. 이 영역이 만든 TurnTrace는 스키마 검증과 구조 검사만 한다.
5. **세션 시각·턴 ID**: 골든 입력에 `turn_id`가 없다. 시험에서는 케이스·턴 번호로 만든다.

## 요청

- 같은 케이스 ID로, 해당 케이스가 쓰는 QuestionBank와 컴포넌트 입력(원본 요구사항 케이스에 이미 있을 것으로 짐작)을 더해 달라. 특히 DialoguePlannerInput이 이미 `question_bank`를 담으므로 turn-engine 쪽 골든과 같은 은행이면 충분하다.
