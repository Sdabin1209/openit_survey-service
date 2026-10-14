# CR-003 공개 API(O06) 대기 목록

정보. 이 영역은 O06이 닫힐 때까지 아래를 설계하지 않았다. 턴 처리는 라이브러리 함수 `handle_turn`과 개발용 CLI로만 노출하고, HTTP에는 `/healthz`, `/readyz`만 있다.

1. 공개 API 4동작의 전송 방식, 인증, 오류 응답, 타임아웃(I10).
2. `turn_id`의 출처. 지금 `handle_turn(..., turn_id=)`는 호출자가 준다. 같은 턴 ID 재전송이 같은 결과를 낸다(brief §4)는 점만 구현했다.
3. `DELIVER_FRONT` 실패의 정의. 프론트 전달을 꽂는 자리(`FrontendSink`)만 있고 기본은 성공이다.
4. `InterventionDecision`이 서비스에 들어오는 경로(직원 콘솔 → 서비스).
5. 문진 대상 참조와 세션 문서의 개인정보 보존 방침.
6. 상태 확인 동작의 응답 내용.
