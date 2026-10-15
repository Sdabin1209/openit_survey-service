# 상태 (2026-10-15, 첫 구현 구간)

## 한 일

1. **설계서와 스키마 패키지**: `docs/design.md`. `schema/`를 설치 가능한 패키지(`openit-survey-schema` 1.0.0, import `survey_schema`)로: `pyproject.toml`, `setup.py`(빌드 때 원본을 `survey_schema/v1/`로 복사), `load_schema(name, version=1)`, `schema_dir(version=1)`, 여러 정수 버전 지원. `CONTRACT_VERSION`과 스키마 파일은 그대로(`git diff`로 확인). 태그는 헤드가 붙인다.
2. **Orchestrator·Session Store**: 처리 표, 한 턴 흐름, 쓰기 확정, 확정 뒤의 일, 실패 신호, 턴 멱등. 반영 효과(9-2)는 순수 함수. SQLite·메모리 저장소.
3. **릴리스 로더·추론 서버·Trace 내보내기**: 포인터 폴링, 승인·해시·계약 버전 검사, 거부 시 이전 유지. outbox와 전송 스레드. `/healthz` `/readyz`. 개발용 CLI.
4. **골든 대조·컨테이너**: 226 케이스 대조(CR-008), Dockerfile, `scripts/build_wheels.sh`, `scripts/smoke.sh`.
5. 문진 스키마 변경 후보 3건은 **적용하지 않고** CR-001에 영향 영역과 함께 정리했다.

## 시험 결과 (실제로 돌린 것)

- `.venv/bin/python -m pytest` : 148 통과.
- 골든: 226 케이스, 처리한 턴 234, 비교 1220쌍 중 1215 일치, 5 불일치 모두 (c) 예시 오류. 골든 표본 1704개 모두 스키마 통과.
- `bash scripts/smoke.sh` : SMOKE OK (이미지 빌드, `/healthz`, `/readyz` 준비됨, 비root, 라벨, 이미지 안의 한 턴, SIGTERM 정상 종료, 필수 설정 없으면 종료 코드 2, 공개 API 경로는 404).

## 하지 못한 것 / 한계

- **실제 이웃과의 통합**: release-gate(I4)·ops-feedback(I6)의 배포된 SDK가 없어 `boundary_contracts.sdk` Protocol을 따르는 대역(`tests/fakes.py`)으로만 시험했다. 실제 SDK가 나오면 팩토리 설정으로 꽂고 같은 시험을 실제 구현으로 돌려야 한다.
- **실제 모델**: GPU·실제 백엔드가 없어 FakeModelSession만 썼다. 추론 서버를 별도 GPU 컨테이너로 나누는 일, 모델 출력 품질은 확인하지 못했다.
- **골든의 한계**: 골든에 문항 세트와 모델·엔진 입력이 없어 실제 turn-engine을 골든 입력으로 돌린 비교는 하지 못했다. Orchestrator는 컴포넌트 출력을 재생해 비교했다(CR-002는 닫음). 입력 조립은 bindings.yaml과 대조했다.
- `docker.exe` 경로(`wslpath`)는 실행해 보지 못했다(이 환경은 docker가 직접 된다).
- CI, digest 게시, deploy 제출은 없다(D24).
- 부하·동시성(여러 프로세스), 세션 문서 암호화·보존 기간 없음.

## 막힌 것 / 헤드에게 필요한 것

- 스키마 패키지 Git 태그(헤드가 붙임).
- CR-001: 스키마 v2 후보 3건(헤드가 모아 한 번에 올릴 때까지 보류).
- O06: 공개 API, 턴 ID 발급, 프론트 전달 실패의 뜻.

## 다음에 할 일

1. 실제 I4·I6 SDK로 통합 시험(대역과 같은 적합성 시험).
2. 실제 백엔드가 생기면 `InferenceServer`에 추가하고, 분리 배포가 필요하면 `ModelSession` 원격 구현.
3. 직원 콘솔·결과 전달·프론트 전달 어댑터(O06 후).
4. 골든 보강본(CR-002)이 오면 실제 turn-engine 대조 추가.

## 후속 구간 (헤드 답변 반영)

- CR-004 해결: `docs/rules/sources/bindings.yaml`과 필드 단위 대조(`tests/test_bindings.py`). 차이 1건(판정기 `affected_followups`)을 고쳤고, 표가 정하지 않은 선택 정책은 design.md 3.1에 근거와 함께 적었다.
- CR-005 결정 반영: 릴리스의 turn-engine·model-runtime MAJOR.MINOR 불일치는 적재 거부(`VERSION_MISMATCH`, `/readyz`에 사유), PATCH는 경고.
- CR-007 결정 반영: 외부 세션·턴 ID는 `Id` 형식이 아니면 `x-`+SHA-256 앞 32자로 봉투에 넣는다(Trace를 잃지 않는다).
- CR-006 반영: model-runtime v0.3.0으로 올림(requirements, wheel 태그, 이미지 라벨). 로더가 역할별 설정·해시를 그대로 넘기고 `SERVING_CONFIG_DIFFERS_BY_ROLE` 거부는 없앴다.
- CR-001은 "스키마 v2 후보 목록"으로 보류(적용 안 함), CR-002는 닫음, CR-003 유지.
- 시험: `.venv/bin/python -m pytest` 168 통과(골든 대조 포함, 1220쌍 중 1215 일치, 5건 (c)). 스모크 SMOKE OK(아래 커밋 뒤 다시 돌림).
- 남은 일: model-runtime v0.3.0 맞춤(헤드 알림 대기), 실제 I4·I6 통합, 실제 백엔드, O06 이후 어댑터.

## 후속 구간 2 (D49·D51)

- 의존 태그: boundary-contracts v1.2.0, model-runtime v0.4.0, turn-engine v0.2.0 (requirements, wheel 빌드, 이미지 라벨). 라벨은 `io.openit.*` 6개.
- I4·I6를 HTTP 클라이언트(`HttpReleaseReader`, `HttpTraceIngest`)로 연결(`SURVEY_RELEASE_READER=http`, URL과 토큰 환경 변수 이름 설정). 번들은 `dest/<릴리스 ID>`.
- 통합 시험: 참조 서버(`tests/ref_servers.py`, boundary-contracts conformance/servers.py 복사)로 번들 수신·적재, 포인터 전환·롤백, UNAVAILABLE/INTEGRITY_ERROR 처리, Trace 전송(재시도·DUPLICATE·DEAD·ID 변환). 스모크도 이미지 안에서 같은 HTTP 경로를 쓴다.
- `pytest` 180 통과, `scripts/smoke.sh` SMOKE OK.
- 한계: 실제 release-gate·ops-feedback 서버와의 E2E는 deploy 몫. model-runtime의 vLLM 백엔드와 어댑터 사전 검사는 서비스에 아직 연결하지 않았다(`SURVEY_MODEL_BACKEND`는 fake만).
