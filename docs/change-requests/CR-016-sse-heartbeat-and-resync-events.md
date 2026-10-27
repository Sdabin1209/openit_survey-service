# CR-016 직원 알림 SSE의 이벤트 `heartbeat`, `resync` 추가

상태: **헤드 확인 요청(계약 추가).** 구현은 하위 호환이다: 기존 클라이언트는 모르는 이벤트 이름을 무시하고, 새 이벤트에는 `id:`가 없어 `Last-Event-ID`를 바꾸지 않는다. 계약 문서(PUBLIC_API.md 43행)는 직접 고치지 않았다.

## 현재 계약

`GET /v1/staff/alerts/stream`의 이벤트 이름은 `alert`, `resolved` 둘이다.

## 구현한 추가 (kiosk-frontend CR-004 요청)

- `event: heartbeat`, `data: {"ts": "<UTC ISO>"}`, `id` 없음. 연결 직후 한 번과 이후 `SURVEY_SSE_HEARTBEAT_SECONDS`(기본 15초)마다. 기존 주석 줄 `: ping`도 같이 보낸다. 브라우저 `EventSource`는 주석 줄을 앱 코드에 보여 주지 않아 조용히 죽은 스트림을 감지하지 못하기 때문이다. 스트림 스레드는 하트비트를 보낼 때 DB를 읽지 않아 알림 DB·상류가 느리거나 죽어도 계속 보낸다(시험 있음).
- `event: resync`, `data: {"reason": "EVENTS_NOT_RESUMABLE"}`, `id` 없음. `Last-Event-ID`로 이어 받을 수 없는 재연결(오래된 이벤트가 지워졌거나 순번이 앞섬)에 한 번. 클라이언트는 `GET /v1/staff/alerts?status=OPEN`으로 다시 맞춘다.

## 제안

PUBLIC_API.md 43행에 두 이벤트 이름과 위 의미를 추가한다.
