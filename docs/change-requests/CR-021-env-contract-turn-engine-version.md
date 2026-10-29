# CR-021: SurveyServiceEnv에 SURVEY_TURN_ENGINE_VERSION을 추가해 달라 (U44, 2026-10-30)

상태: **무효(계약 3.0.0 전환으로 설정 SURVEY_TURN_ENGINE_VERSION이 없어졌다, ADR-0043).** 아래는 당시 요청이다.

## 사실
- 서비스는 설정 `SURVEY_TURN_ENGINE_VERSION`(릴리스 매니페스트의 `turn_engine_version`과 MAJOR.MINOR를 비교하는 기준값, 기본 0.4.3, D109, ADR-0028)을 읽는다.
- 계약 `SurveyServiceEnv`(v2.5.1)의 알려진 SURVEY_* 목록에 이 이름이 없다. 목록은 `settings.py`가 읽는 이름과 같아야 한다고 스키마 설명이 적고 있다.
- 서비스가 시작 때 계약 스키마로 설정을 검증하고 모르는 SURVEY_* 이름을 거부하므로(ADR-0038), 이 이름은 계약에 들어갈 때까지 서비스 코드의 예외 목록(`_NOT_YET_IN_CONTRACT`)으로만 통과한다.

## 요청
`SurveyServiceEnv`의 `propertyNames` 목록에 `SURVEY_TURN_ENGINE_VERSION`을 추가한다(값 형식은 `^v?[0-9]+\\.[0-9]+\\.[0-9]+$`를 제안; 서비스는 앞의 v를 떼고 쓴다). 반영되면 서비스는 예외 목록을 지운다.
