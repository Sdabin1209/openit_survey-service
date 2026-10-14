# CR-005 릴리스의 엔진·런타임 버전이 설치본과 다를 때

- 상태: 질문. 규칙이 없어 정책을 만들지 않고 가장 약한 동작을 골랐다.
- 사실: `ReleaseManifest`에 `turn_engine_version`, `model_runtime_version`이 기록된다(게이트가 평가에 쓴 버전). 서비스 이미지는 자기 버전의 turn-engine·model-runtime을 설치한다. INTERFACES는 거부해야 하는 경우로 `runtime_contract_version` 불일치만 적는다.
- 현재 동작: 다르면 경고 로그와 상태(`/readyz`의 본문)에 기록하고 적재한다.
- 위험: turn-engine은 "유효한 입력 하나라도 출력이 달라지면 MINOR"(boundary §4)이므로 게이트가 승인한 결과와 서비스 동작이 달라질 수 있다.
- 질문: (a) 지금처럼 경고만, (b) 다르면 적재 거부, (c) MAJOR/MINOR 중 일부만 거부. 헤드가 정해 달라.
