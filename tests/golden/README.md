# 정답 예시(golden)

출처: HealthCheckSchema `work/testcases/case_shapes/`에서 이 영역에 해당하는 계약만 추출(헤드 전달, 2026-10-15). 오케스트레이터·프론트·세션 저장·로그·직원 콘솔·결과 전달·Trace 계약의 입출력 예시.

- 요구사항 케이스마다 턴별 `(입력, 출력)` 예시가 있다. **요구 작성자가 만든 정적 예시이고 실행 결과가 아니며** `correctness_claim: false`다.
- 같지 않다고 바로 구현이 틀렸다는 뜻이 아니다. 차이는 (a) 구현 결함 (b) 규칙 해석 차이 (c) 예시 자체의 오류 중 하나다. 분류해서 (a)는 고치고 (b)·(c)는 근거와 함께 변경 요청에 남긴다.
- 알려진 예시 오류는 turn-engine의 `tests/golden_known.json`과 `docs/change-requests/CR-009`에 일부 정리되어 있다(턴 단위 대기 ID 자리표시자, 고정 문장 본문 등).
