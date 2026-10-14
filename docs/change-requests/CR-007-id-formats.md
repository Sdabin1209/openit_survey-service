# CR-007 세션 ID·턴 ID의 형식이 문진 스키마와 경계 계약에서 다르다

> **상태: 결정·반영(CONVENTIONS 10절). 세션 ID·턴 ID가 Id 형식이면 그대로, 아니면 'x-' + 원본 UTF-8 SHA-256 앞 32자를 봉투에 넣는다. 지금은 DEAD 처리하지 않는다. 시험: tests/test_trace_export.py.**


- 상태: 확인 요청(정보).
- 사실: 문진 스키마의 `SessionId`는 "비어 있지 않은 문자열"뿐이다(`FrontendMessage.session_id`, 골든은 `S-TEST`). `TurnTrace.turn_id`도 문자열이다. 그런데 `TraceEnvelope.session_id`·`turn_id`는 경계 계약의 `Id`(`[A-Za-z0-9][A-Za-z0-9._:-]*`, 최대 128자)다. 공백·한글·긴 ID가 세션 ID로 오면 봉투가 스키마를 어겨 I6에서 `INVALID_RECORD`가 된다.
- 현재 동작: 서비스는 세션 ID를 가공하지 않는다. 전송 전 검증에서 봉투가 어기면 그 Trace는 보내지 않고 `DEAD`(`LOCAL_INVALID`)로 남기고 `/readyz`에 개수가 나온다. 턴 처리는 영향이 없다.
- 요청: (a) 공개 API(O06)가 세션 ID·턴 ID를 `Id` 형식으로 발급한다고 정하거나, (b) 문진 스키마의 `SessionId`에 같은 패턴을 두거나(스키마 변경, 정수 버전 영향), (c) 봉투가 원본 ID를 해시한 값을 쓰도록 한다. 턴 ID를 누가 만드는지도 O06과 함께 정해야 한다.
