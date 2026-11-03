# 문진 스키마 변경 이력

이 디렉터리(`schema/`)는 survey-service 영역이 소유한다(D29). 패키지 버전(`pyproject.toml`)은 배포 이름 `openit-survey-schema`의 SemVer이고, 아래 "정수 버전"과 다르다.

## 정수 버전 규칙 (계약 CONVENTIONS 12절, G11)

`schema/CONTRACT_VERSION`(정수, 현재 1)은 릴리스의 `runtime_contract_version`이고 문항 은행·칸 키를 학습한 모델 패키지의 호환 번호다. TurnTrace를 쓰는 survey-service와 읽는 ops-feedback은 같은 정수를 설치해야 한다.

- **올려야 하는 변경**(읽는 쪽이 깨지는 변경): 키 삭제, 이름 변경, enum 축소, 필수 추가.
- **같은 정수 안에서 허용하는 변경**: 추가만(선택 필드, enum 값 추가, 설명).
- 올릴 때는 반영 전에 헤드에 알린다(D21). 올리면 survey-service와 ops-feedback이 같은 정수를 함께 배포한다.
- 주의: enum 값 추가는 "추가"이지만 읽는 쪽이 enum을 닫힌 집합으로 검증하면 새 값을 거부한다. 같은 정수 안의 추가라도 읽는 쪽 영역에 알린다.

## 정수 버전 1 안의 비호환 변경 이력 (G11)

정수 버전을 올리지 않고 들어간 변경 중 읽는 쪽에 영향을 줄 수 있는 것. 이 목록은 이 영역이 알고 있는 것이며, 읽는 쪽이 실제로 깨졌는지는 확인하지 못했다.

| 패키지 버전 | 변경 | 읽는 쪽 영향 | 근거 |
| --- | --- | --- | --- |
| 2.0.0 | `SessionResult.skipped`(문자열 목록) 폐지, `skipped_details`만 남김 | 결과를 읽는 병원 시스템이 `skipped_details`로 옮겨야 한다(키 삭제 = 규칙상 깨지는 변경). 모델이 보지 않는 출력 형식이라 정수를 올리지 않았다 | docs/adr/ADR-0044.md |
| 2.1.0 | `TurnFailureSignal`·`SessionState`·`StaffSessionView`·`TurnTrace`에 `pause_reason`(선택) 추가 | 추가만. 영향 없음 | CR-H002, docs/adr/ADR-0022.md |
| 3.0.0 | 계약 3.0.0 전환(`ActionExtraction` 등)에 맞춘 개정 | 위 2.0.0과 같은 묶음 | docs/adr/ADR-0044.md |

## 같은 정수 버전 1 안의 추가

| 날짜 | 변경 | 읽는 쪽 영향 |
| --- | --- | --- |
| 2026-11-03 | `SessionResult.skipped_details[].reason`에 `REASK_LIMIT` 추가(되묻기 한도로 빈칸). `SessionStore`·`TurnTrace`·`DispatchInput`의 `SkipReason`에 `REASK_LIMIT` 추가(진행 기록의 건너뛴 문항 사유) | enum 값 추가. 닫힌 enum으로 검증하는 읽는 쪽(ops-feedback의 TurnTrace 읽기, 병원 시스템의 결과 읽기)은 새 값을 받아들여야 한다. 헤드가 해당 영역에 알린다. 근거 docs/adr/ADR-0054.md |
| 2026-11-03 | `SessionResult`에 선택 필드 `acknowledged_by`(문자열), `acknowledged_at`(UTC 시각) 추가(결과 확인 표시, U83). 확인 전에는 없다 | 선택 필드 추가라 영향 없음. 근거 docs/adr/ADR-0056.md |

위 표의 2.0.0·2.1.0·3.0.0 행의 날짜와 세부는 `docs/adr/`와 `docs/change-requests/`가 정본이다. 이 표는 그 요약이며, 읽지 못한 이력이 있으면 빠졌을 수 있다(추정).
