# 경계 문서: survey-service

인터페이스 번호의 시그니처·오류·멱등성은 `../boundary-contracts/INTERFACES.md`와 `CONVENTIONS.md`가 정한다. 이 문서는 그 안에서 이 영역이 지키는 것이다.

## 1. 소유하는 것

- 소유 저장소: **Session Store**(서비스 내부, 기본 SQLite 파일 `SURVEY_SESSION_DB`)와 **Trace outbox**(SQLite 파일 `SURVEY_OUTBOX_DB`). 이웃은 두 파일을 읽지 않는다.
- 유지하는 것: `schema/`(문진 스키마, 정수 버전 `CONTRACT_VERSION`=1). 변경은 다른 영역의 변경 요청을 받아 반영하고, 계약 버전을 올리는 변경은 반영 전에 헤드에 알린다(D21).

## 2. 제공하는 것

| 제공 | 내용 |
|---|---|
| 문진 스키마 패키지 | 배포 이름 `openit-survey-schema`, import `survey_schema`. `load_schema(name, version=1)`, `schema_dir(version=1)`(model-runtime `load(schema_dir=...)`에 넘기는 `<계약명>.json`이 있는 디렉터리), `contract_version_dir`, `available_versions`, `current_version`, `list_schemas`. 정수 버전별 디렉터리 `survey_schema/v1/`. 설치: Git 태그 + `#subdirectory=schema`(태그는 헤드가 붙인다). |
| SDK 인터페이스 | **없다**(brief §2). 이웃이 서비스의 내부 저장소를 읽지 않는다. 따라서 이 영역의 메모리 구현(가짜 SDK)도 없다. 이웃이 이 영역을 흉내 낼 일은 공개 API(I10)가 정해진 뒤의 몫이다. |
| HTTP | `GET /healthz`, `GET /readyz`만. 공개 API(I10)는 **O06 대기**. |
| 개발용 CLI | `python -m survey_service.cli`(공개 API가 아니다) |

## 3. 소비하는 것과 지원 버전

| 소비 | 지원 |
|---|---|
| 문진 스키마(자기 `schema/`) | 정수 버전 **1** 한 값. `ReleaseManifest.runtime_contract_version`이 이 집합 밖이면 적재를 거부하고 이전 릴리스를 유지 |
| 경계 계약 | `boundary-contracts` **v1.1.0**(`pipeline_schema_version` "1.1"). 같은 MAJOR의 모르는 필드는 보존(`ext`)하고 판단에 쓰지 않는다 |
| I4 ReleaseReader | `get_active_pointer`(폴링), `get_release`, `get_decision`, `load_bundle`. 턴 처리 중에는 부르지 않는다 |
| I6 TraceIngest | `send(TraceEnvelope)`. 턴마다 한 건, 영속 outbox를 거쳐 |
| I8 turn-engine | **v0.2.0** (릴리스의 `turn_engine_version` MAJOR.MINOR가 다르면 적재 거부, PATCH는 경고) `normalize`, `judge`, `write_record`, `plan` |
| I9 model-runtime | **v0.3.0** (같은 규칙. `load`는 역할별 `serving_configs`·`expected_serving_config_hashes`) `load`, `parse`, `generate`, `last_call_info` |

버전 고정: Git 태그(`requirements.txt`), 서드파티는 `requirements.lock`, 이미지는 `git archive <태그>`로 만든 wheel. 이미지 라벨: `org.opencontainers.image.revision`(커밋), `io.openit.contracts.boundary`, `io.openit.contracts.survey-schema`, `io.openit.turn-engine`, `io.openit.model-runtime`.

## 4. 이 영역이 이웃에게 지키는 것

- 포인터가 바뀌면 **새 세션부터** 새 릴리스를 쓴다. 진행 중 세션은 자기 릴리스로 끝까지 간다.
- 지원하지 않는 `runtime_contract_version`, 라이브러리 MAJOR.MINOR 불일치, 승인(PASS) 기록이 없거나 이 조합을 가리키지 않는 판정, 해시 불일치, 모델 적재 거부는 적재하지 않고 이전 릴리스를 유지한다.
- Trace: 턴마다 `TraceEnvelope`(릴리스 ID, 계약 버전 포함) 한 건. 전송은 턴 처리와 분리, `UNAVAILABLE`일 때만 재시도, 최소 1회 전달. 같은 (세션, 턴)은 한 번만 넣는다.
- 같은 (세션 ID, 턴 ID)의 재전송은 같은 결과(이중 저장 없음). 같은 턴 ID에 다른 입력이면 `TurnIdConflict`.
- 한 턴 동안 문항 저작·합성 데이터·모델 개발·게이트를 호출하지 않는다.

## 5. 이웃이 이 영역에 기대해서는 안 되는 것

- 공개 API의 형태·인증·타임아웃(O06). 쓰기 반영 여부를 알 수 없는 커밋(`write_outcome: UNKNOWN`)은 결과를 고정하지 않으므로 같은 턴 ID 재시도가 다시 처리될 수 있다.
- 실패한 턴(`NOT_COMMITTED`)의 결과도 같은 턴 ID로는 같게 돌아온다. 다시 시도하려면 새 턴 ID가 필요하다 [제안, CR-003의 `turn_id` 항목과 함께 O06에서 확정].
- 직원 콘솔, 결과 전달, 프론트 전달의 실제 연동(접점 Protocol만 있다).

## 6. 장애 영향

| 상황 | 증상 | 영향 | 복구 |
|---|---|---|---|
| I4 일시 장애(`UNAVAILABLE`) | 폴링 실패 로그, `release_loader.last_poll_error` | 새 릴리스 반영이 늦어진다. 서비스와 진행 중 세션은 영향 없음 | 다음 주기에 자동 재시도 |
| 새 릴리스 적재 거부 | `rejections`에 코드, 이전 릴리스 유지 | 새 릴리스가 안 켜진다 | 게이트·모델 쪽을 고치고 새 포인터 |
| 활성 릴리스가 한 번도 적재되지 않음 | `/readyz` 503 `no_active_release` | 새 세션을 받을 수 없다 | 포인터·릴리스 확인 |
| I6(수집) 장애 | outbox `pending` 증가, `consecutive_unavailable` | **턴 응답에는 영향 없음**. Trace 전달이 늦어진다 | 수집 복구 후 자동 전송 |
| outbox 디스크 오류 | Trace 유실(턴은 정상), `trace_enqueue_failed` 카운터, `/readyz` 503 `outbox_db` | 해당 Trace 없음(최소 1회 보장 밖) | 디스크 복구 |
| Session Store 오류 | 쓰기 확정 실패 → `NOT_COMMITTED` 신호, 응답 없음. 조회 실패는 로그 없는 실패 신호 | 그 턴은 반영되지 않는다. 확정 뒤의 일은 하지 않는다(위험 알림도 나가지 않는다, 아래 주의) | 저장소 복구 후 새 턴 |
| 직원 콘솔 전달 실패 | `DELIVERY_FAILED` 신호, 응답은 그대로 | 쓰기는 반영됨. 직원이 알림을 받지 못함 | 프론트가 신호로 대처(규칙 J3) |
| 모델 출력이 깨짐 | model-runtime의 출력 보호(대체 출력) | 턴은 실패하지 않음 | model-lab이 `*_raw`로 품질 확인 |
| 컨테이너 재시작 | 진행 중 세션 유지(SQLite), 릴리스 재적재 | 재적재 전까지 `/readyz` 503 | 자동 |

**주의(안전)**: 쓰기 확정이 실패한 턴에서는 위험 신호가 있어도 직원 알림을 보내지 않는다. 규칙(preconditions 0-1.3, J8f)이 그렇게 정한다. 프론트는 `NOT_COMMITTED` 신호를 받으면 사용자에게 다시 말하게 하거나 직원 호출로 대처해야 한다. 이 정책이 위험 상황에서 충분한지는 도메인 소유자가 확인할 일이다.

## 7. 소비자별 메모

| 이웃 | 메모 |
|---|---|
| release-gate | 활성 포인터는 값싸게 자주 읽힌다(기본 5초). 매니페스트 `content_hash`와 `question_set.content_hash`를 서비스가 다시 계산해 검증한다. `adapters.LORA_A/B.serving_config`가 달라지면 거부된다(model-runtime 0.3.0 전까지, CR-006). `turn_engine_version`·`model_runtime_version`은 이미지에 설치된 것과 MAJOR.MINOR가 같아야 한다. |
| ops-feedback | 봉투의 `release_id`는 세션이 묶인 릴리스다. `Id` 형식이 아닌 세션 ID·턴 ID는 `x-`+SHA-256 앞 32자로 바꿔 봉투에 넣는다(CONVENTIONS 10절). 원본은 `trace` 본문에 있다. 같은 (세션, 턴)이 두 번 올 수 있다(최소 1회). |
| 다른 영역의 문진 스키마 소비 | `openit-survey-schema`를 설치한다. 정수 버전을 여러 개 지원하면 `load_schema(name, version=N)`. |
