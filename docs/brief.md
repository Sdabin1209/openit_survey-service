# 영역 브리프: 문진 서비스 (survey-service)

> **계약 상태.** 영역 사이 계약은 `../boundary-contracts`(헤드 소유)에 있고, 이 브리프의 계약 이름은 `head/docs/STRUCTURE.md`의 제안이다. 초안이 있는 것은 ReleaseManifest, GateDecision, TraceEnvelope뿐이고 나머지 필드는 헤드가 사용자와 논의해 확정 중이다. 확정 전에는 **계약에 의존하지 않는 부분**(내부 설계, 도메인 모델, 테스트 틀)만 진행한다. 계약이 부족하다고 느끼면 임의로 가정하지 말고 `docs/change-requests/`에 쓴다.
> **읽기.** `../boundary-contracts`는 읽기 전용이다. `schema/`는 이 영역이 소유하는 문진 스키마(스키마만 남긴 최소 구성)이며 구현이 가져가는 계약은 `schema/schemas/<계약명>.json`, 컴포넌트별 입출력 대응은 `schema/COMPONENTS.yaml`, 정수 버전은 `schema/CONTRACT_VERSION`이다.
> **버전(D14, D19).** 문진 스키마는 정수 버전(`CONTRACT_VERSION`, 현재 1)에 정확히 일치해야 한다. 경계 계약은 `MAJOR.MINOR`이며 값은 `boundary-contracts`가 정한다. 불변 산출물은 `ArtifactRef`(id, version, content_hash)로 가리킨다.
> **업무 규칙.** 이 영역이 구현하거나 의존하는 업무 요구는 `docs/rules/`에 있다(헤드가 전달, 출처는 `docs/rules/SOURCE.md`). 규칙 문서에 없는 정책은 임의로 만들지 말고 `docs/change-requests/`에 쓴다.

## 1. 목적과 범위

- 목적: 문진 대상의 발화 한 턴을 정형 문진 값으로 바꾸고, 다음 행동에 맞는 응답 문장을 돌려준다.
- 하는 일: 세션 시작·발화 처리·종료, 한 턴의 순서 관리(Orchestrator), 릴리스 묶음 적재(릴리스 로더), 턴 단위 Trace 내보내기, 추론 서버 운영.
- 하지 않는 일: 판단 로직 구현(turn-engine의 몫), 모델 로딩·프롬프트·디코딩(model-runtime의 몫), 문항·모델 승인(게이트의 몫), Frontend 화면(다른 팀).

## 2. 소유하는 것

- 소유 저장소: Session Store(서비스 내부).
- 유지하는 것: `schema/`(문진 스키마). 이 리포지토리에 포함되어 함께 버전 관리된다(D29). 다른 영역의 변경 요청을 받아 그 저장소의 절차(원본, 의미, 예시, `python3 verify.py`)대로 반영한다. 계약 버전을 올리는 변경은 반영 전에 헤드에 알린다(D21). 이 저장소 수정은 survey-service 에이전트에게만 허용되는 예외다.
- 제공하는 SDK 인터페이스: 없음. 이웃이 서비스의 내부 저장소를 읽지 않는다.
- 제공하는 서비스 API: 공개 API(세션 시작, 발화 전달, 세션 종료, 상태 확인). 같은 턴 ID로 다시 보내면 결과가 같아야 한다. 형태·인증·전송 방식은 D55로 정해져 있고(`boundary-contracts/PUBLIC_API.md`, 구현은 `survey_service/api.py`, 해석은 CR-009, 엔드포인트 목록은 docs/boundary.md §3), 같은 턴 ID 멱등은 구현되어 있다. 문진 대상 참조 등 남은 부분의 O06 상태는 미확인.
- Trace 전송: 로그 수집 SDK로 `TraceEnvelope`(TurnTrace 포함)를 보낸다. 릴리스 ID는 봉투에 붙인다.

## 3. 소비하는 것

- 소비하는 계약: 런타임 계약 전부(기존 12개 컴포넌트), `ReleaseManifest`, `GateDecision`.
- 소비하는 SDK 인터페이스: 릴리스 읽기 SDK(활성 묶음 조회, release-gate가 배포), TraceIngest(I6, ops-feedback이 배포). 라이브러리로 turn-engine, model-runtime.
- 지원하는 계약 버전: 문진 스키마 정수 버전 한 값. 지원하지 않는 버전의 묶음은 적재를 거부한다.

## 3-1. 경계 명세 (블랙박스 기준)

상대 영역은 블랙박스다. 아래 인터페이스 번호의 시그니처, 오류, 멱등성은 `../boundary-contracts/INTERFACES.md`와 `CONVENTIONS.md`가 정한다. 이 명세 밖의 것을 상대에게 기대하거나 가정하지 않는다.

- 내는 것: **I6**에 `TraceEnvelope` 전송(턴마다), **I10** 공개 API(D55 구현, docs/boundary.md §3).
- 받는 것: **I4**(활성 포인터와 릴리스 묶음), **I8**, **I9**.
- 해야 하는 것: 포인터가 바뀌면 새 세션부터 새 릴리스를 쓴다. 지원하지 않는 `runtime_contract_version`의 릴리스는 적재하지 않고 이전 것을 유지한다. 전송은 영속 버퍼를 거쳐 턴 처리와 분리한다. `schema/`(문진 스키마)를 유지한다.

- 문진 스키마를 **설치 가능한 패키지**로 낸다(Git 태그, 정수 버전별 스키마 디렉터리 포함). 다른 영역이 스냅샷을 복사하지 않고 설치해 쓰며, 여러 정수 버전을 동시에 지원하는 소비자(ops-feedback)가 버전별로 스키마를 얻을 수 있어야 한다.

## 4. 상위 수용 기준

- 게이트가 승인한 묶음만 적재하고, 승인 기록이 없는 묶음은 거부한다.
- 같은 세션 ID·턴 ID의 재전송이 같은 결과를 낸다(이중 저장 없음).
- 한 턴을 처리하는 동안 문항 저작·합성 데이터·모델 개발·게이트 영역을 호출하지 않는다.
- 모든 턴이 `TurnTrace`를 남기고, 봉투에 적용된 릴리스 ID와 계약 버전이 들어간다.
- 모델은 해석과 문장 생성만 하고 상태 판정·저장 결정은 코드가 한다.
- 새 릴리스로의 전환은 새 세션부터 적용한다(O13, 기본 권고).

## 5. 비기능 요구

- 지연: 미정(사용자와 Frontend 팀이 정한다). 설계서에 수치가 없다.
- 가용성: 서버 전체 중단은 비보증(D10). 서비스 단위 장애 격리는 영역이 설계한다.
- 자원: 추론 서버는 GPU를 쓰는 별도 컨테이너.
- 보안·개인정보: 문진 대상 참조가 무엇인지 미정(O06). 로그는 원본을 ops-feedback만 읽는다.

## 6. 건드리면 안 되는 경계

- 릴리스 레지스트리와 로그 저장소에는 읽기·수집 SDK로만 접근한다.
- 서비스 안의 Question Bank는 문항 저장소의 서빙 사본이며 원본이 아니다. 릴리스 묶음으로만 갱신한다.
- 다른 영역 리포지토리의 코드를 가져오지 않는다.

## 7. 이 영역에 해당하는 열린 결정

- O04 Trace 내보내기의 전달 보장: 확정: 최소 1회 전달과 턴 ID로 멱등 처리(D16).
- O06 공개 API 형태와 세션 방침: 공개 API 형태는 D55로 구현됨(docs/boundary.md §3). O06 항목 자체를 닫았는지(세션 방침, 문진 대상 참조 포함)는 미확인.
- O13 모델 전환 시점: 기본은 새 세션부터.
- O10 Session Store의 실제 기술: 영역이 정한다.

## 8. 시드

Frontend 연동 계층, Orchestrator(LangGraph 그래프, `survey_service/graph.py`), 모델 추출 호출(LoRA-A)과 응답 생성 호출(LoRA-B), turn-engine의 `validate_extraction` 호출과 `QuestionFlowTable` 조립, 릴리스 로더, 트레이스 내보내기, 추론 서버 클라이언트. 현행 구조는 `docs/design.md` 16절. 시드는 구속하지 않는다.

## 9. 요구하는 산출물

- 영역 설계서(`docs/design.md`), 경계 문서(`docs/boundary.md`)
- 입출력 예시와 수용 테스트
- 에이전트용 `CLAUDE.md`, 런북, 변경 이력
