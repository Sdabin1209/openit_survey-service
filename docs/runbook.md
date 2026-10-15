# 런북: survey-service

## 1. 띄우기

필수 환경 변수 세 개가 없으면 시작하지 않는다(종료 코드 2).

| 변수 | 예 |
|---|---|
| `SURVEY_RELEASE_READER` | `http`(기본 형식: boundary-contracts의 `HttpReleaseReader`) 또는 팩토리 `모듈:함수` |
| `SURVEY_TRACE_INGEST` | `http`(`HttpTraceIngest`) 또는 팩토리 `모듈:함수` |
| `SURVEY_MODEL_BACKEND` | 지금은 `fake`(개발·스모크 전용)뿐. 실제 백엔드는 model-runtime에 아직 없다 |

HTTP 클라이언트 설정(영역 사이 호출은 HTTP, D49): `SURVEY_RELEASE_READER_URL`, `SURVEY_TRACE_INGEST_URL`(base URL), `SURVEY_RELEASE_READER_TOKEN_ENV`, `SURVEY_TRACE_INGEST_TOKEN_ENV`(토큰이 들어 있는 **환경 변수의 이름**, 기본 `SURVEY_RELEASE_READER_TOKEN`, `SURVEY_TRACE_INGEST_TOKEN`), `SURVEY_HTTP_TIMEOUT_SECONDS`(10). 토큰 값은 환경 변수로만 받고 이미지·리포지토리·로그에 넣지 않는다. 클라이언트가 던지는 `BoundaryError`는 로더와 outbox 규칙으로 처리된다: UNAVAILABLE이면 로더는 이전 릴리스를 유지한 채 다음 주기에 같은 포인터를 다시 보고, outbox는 UNAVAILABLE일 때만 재시도한다. 그 밖의 오류(INTEGRITY_ERROR, UNSUPPORTED_VERSION, FAILED_PRECONDITION 등)는 릴리스 거부 또는 Trace DEAD다.

공개 API: `SURVEY_API_TOKEN_ENV`(토큰이 든 환경 변수의 이름, 기본 `SURVEY_API_TOKEN`. **토큰이 없으면 시작하지 않는다**), `SURVEY_STAFF_API_TOKEN_ENV`(직원 개입용 토큰이 든 환경 변수의 이름, 기본 `SURVEY_STAFF_API_TOKEN`. 키오스크 토큰과 달라야 하고 없으면 시작하지 않는다), `SURVEY_SESSION_IDLE_SECONDS`(300, 0이면 유휴 만료 끔), `SURVEY_API_MAX_INFLIGHT`(32), `SURVEY_API_MAX_UTTERANCE_CHARS`(1000). 프론트에는 `https://<서비스>/v1/sessions...`와 토큰을 운영 배포 때 전달한다(TLS는 앞단). 모델을 추론 컨테이너로 분리하려면 `SURVEY_INFERENCE_URL`, `SURVEY_INFERENCE_TOKEN_ENV`, `SURVEY_INFERENCE_TIMEOUT_SECONDS`(호출 하나, 4), `SURVEY_INFERENCE_RETRIES`(1), `SURVEY_INFERENCE_LOAD_TIMEOUT_SECONDS`(600)를 준다(`SURVEY_MODEL_BACKEND`는 비워도 된다). 추론 서버는 model-runtime 영역의 서빙 이미지이고 규약은 `docs/inference-server-spec.md`다. `/readyz`의 `inference`로 호출 실패·재시도·타임아웃·보호 건수를 본다(타임아웃이 늘면 추론 서버 부하, `outcome_fallback`이 늘면 모델 품질 문제).

선택: `SURVEY_STAFF_CONSOLE`, `SURVEY_RESULT_DISPATCHER`(팩토리, 없으면 알림·결과 전달은 실패로 기록), `SURVEY_PORT`(8080), `SURVEY_SESSION_DB`, `SURVEY_OUTBOX_DB`, `SURVEY_WORK_DIR`(`/data/...`), `SURVEY_RELEASE_POLL_SECONDS`(5), `SURVEY_KEEP_RELEASES`(2), `SURVEY_EXPORT_INTERVAL_SECONDS`(1), `SURVEY_EXPORT_BACKOFF_MAX_SECONDS`(60), `SURVEY_OUTBOX_KEEP_SENT_HOURS`(24), `SURVEY_LOG_LEVEL`. 토큰은 I4·I6 구현이 자기 환경 변수로 받는다. `/data`는 볼륨으로 둔다.

## 2. 상태 보기

- `GET /healthz`: 200이면 프로세스가 산다.
- `GET /readyz`: 200이면 새 세션을 받을 수 있다. 503이면 `problems`를 본다.

| problems | 뜻 | 조치 |
|---|---|---|
| `no_active_release` | 활성 릴리스를 적재하지 못했다 | `release_loader.rejections`의 코드, `last_poll_error` 확인 |
| `session_db` | 세션 DB를 열 수 없다 | 볼륨, 디스크, 권한 |
| `outbox_db` | outbox를 열 수 없다 | 같음 |

`release_loader.rejections[].code`: `UNSUPPORTED_VERSION`(릴리스의 계약 버전을 이 이미지가 지원하지 않음: 맞는 이미지로 올리거나 릴리스를 롤백), `NO_APPROVAL`(게이트 승인 기록 없음·다른 조합), `VERSION_MISMATCH`(릴리스의 turn-engine·model-runtime MAJOR.MINOR가 이미지와 다름: 맞는 이미지로 올리거나 릴리스를 롤백. PATCH 차이는 경고만), `INTEGRITY_ERROR`(해시 불일치), `INVALID_RECORD`(스키마 위반), `ServingConfigMismatch`·`UnsupportedServingConfig`·`ContractVersionMismatch`·`BackendNotAvailable`(model-runtime 거부). 거부된 포인터 버전은 다시 시도하지 않는다. 새 포인터(수정한 릴리스 또는 롤백)가 오면 다시 본다. `release_loader.unavailable_releases`는 진행 중 세션이 쓰는데 적재하지 못한 릴리스다(그 세션의 턴은 첫 단계 실패).

## 3. Trace 전송

`trace_outbox`: `pending`(보낼 것), `sent`, `dead`(재시도하지 않는 실패), `oldest_pending_age_seconds`, `consecutive_unavailable`.

- pending이 늘고 `last_error`가 `UNAVAILABLE`이면 수집(ops-feedback)이 내려가 있다. 턴에는 영향이 없고 복구되면 자동으로 보낸다.
- dead 확인: `sqlite3 $SURVEY_OUTBOX_DB "SELECT session_id, turn_id, last_error FROM outbox WHERE state='DEAD'"`. `LOCAL_INVALID`는 이 서비스가 만든 봉투가 스키마를 어긴 것(결함이다. 세션·턴 ID는 `x-` 변환으로 막혀 있다). `INVALID_RECORD` 등은 수집이 거부한 것이다. 고친 뒤 다시 보내려면 해당 행을 `state='PENDING', next_attempt_at=0, attempts=0`으로 되돌린다.
- 전달 완료 행은 `SURVEY_OUTBOX_KEEP_SENT_HOURS` 뒤에 지운다.

## 4. 한 턴이 실패했을 때

`dialogue_log`의 `turn_result.failed_steps`와 프론트에 간 `turn_failure_signal`을 본다.

| failed_steps | 뜻 | 쓰기 | 조치 |
|---|---|---|---|
| BEFORE_COMMIT `LoRA-A` / `NORMALIZE` / `JUDGE` / `RECORD_WRITE` / `PLAN` | 그 컴포넌트가 예외. 입력 조립·엔진 결함 | 반영 안 됨 | 로그(`step=`)와 비식별 입력으로 turn-engine·model-runtime에 보고 |
| BEFORE_COMMIT `COMMIT` | 저장소가 쓰기를 거부(DB 오류, revision 충돌, 반영할 수 없는 결정) | 반영 안 됨 | `commit_failed.*` 카운터와 로그. `UNKNOWN`이면 같은 턴 ID 재시도가 다시 처리된다 |
| AFTER_COMMIT `DELIVER_STAFF` | 직원 콘솔 전달 실패 | 반영됨 | 위험·호출이면 직원에게 직접 알린다 |
| AFTER_COMMIT `LoRA-B` | 응답 생성 실패(인프라) | 반영됨 | 모델 서버 확인 |
| AFTER_COMMIT `DELIVER_FRONT` | 프론트 전달 실패(O06 대기) | 반영됨 | — |

## 5. 개발·시험

```
python3 -m venv .venv && .venv/bin/pip install -e ../boundary-contracts ../turn-engine ../model-runtime -e schema -e . pytest pyyaml
.venv/bin/python -m pytest                 # 단위·통합·골든 대조
bash scripts/smoke.sh                      # 이미지 빌드 + /healthz /readyz + 비root + 라벨 + 이미지 안의 한 턴
```

개발용 CLI(세션은 SQLite 파일에 이어진다, 파서가 낼 항목은 `--items`로 직접 준다):
`python -m survey_service.cli --bank bank.json --db /tmp/s.db turn --session S1 --start`
`... turn --session S1 --utterance "안 피움" --items '[{"item_no":0,"target":{"question_id":"Q1","repeat_key":"","slot_id":"상태"},"value":"안 피움","source_text":"안 피움","intent":"ANSWERED_INTENT","progress_op":null,"guide_id":null}]'`

스모크는 고정 포트를 쓰지 않는다(`-p 127.0.0.1::8080`, `docker port`). docker가 없으면 `docker.exe`를 쓴다(경로 변환은 `wslpath`, 이 경로는 아직 해 보지 못했다).

## 6. 이미지

`bash scripts/build_wheels.sh`가 `git -C ../<이름> archive <태그>`로 소스를 꺼내 wheel을 만든다(`BOUNDARY_TAG`, `TURN_ENGINE_TAG`, `MODEL_RUNTIME_TAG`로 바꾼다). 이어서 `docker build --build-arg COMMIT=... BOUNDARY_TAG=... TURN_ENGINE_TAG=... MODEL_RUNTIME_TAG=... SCHEMA_CONTRACT_VERSION=...`(smoke.sh가 하는 그대로). CI는 digest를 게시하고 deploy에 제출한다(D24, 아직 CI는 없다).

## 6-1. 이미지 조합과 릴리스 정책 (D46)

릴리스의 `turn_engine_version`·`model_runtime_version`은 MAJOR.MINOR가 이미지에 설치된 것과 같아야 적재된다(PATCH 차이는 경고). model-runtime이 0.3→0.4로 MAJOR.MINOR가 바뀌었으므로, **게이트가 평가에 쓴 이미지(릴리스 매니페스트에 기록된 버전)와 같은 라이브러리 조합의 survey-service 이미지**를 배포해야 한다. 조합이 어긋난 릴리스는 `VERSION_MISMATCH`로 거부되고 이전 릴리스가 유지된다. 이미지 라벨은 6개다: `io.openit.service`, `io.openit.revision`, `io.openit.contracts.boundary`, `io.openit.contracts.survey-schema`, `io.openit.turn-engine`, `io.openit.model-runtime`(D50). 현재 조합: boundary-contracts v1.2.0, turn-engine v0.2.0, model-runtime v0.4.0(기본 백엔드는 fake. vLLM 백엔드와 어댑터 사전 검사는 이 서비스에 아직 연결하지 않았다).

## 7. 스키마 패키지 릴리스

태그는 헤드가 붙인다. 검증: `python -m pytest tests/test_schema_package.py`. 새 정수 버전: `schema/vN/`(schemas, CONTRACT_VERSION)를 만들면 같은 wheel에 함께 들어간다. 계약 버전을 올리는 변경은 반영 전에 헤드에 알린다.


## 8. compose 스모크 (서비스 + 추론 + 가짜 게이트)

`bash scripts/smoke_compose.sh`: 서비스 이미지, 가짜 추론 이미지(model-runtime의 FakeModelSession 경로, 모델 없음), 가짜 게이트/수집 서버를 `tests/compose/compose.smoke.yaml`로 같이 띄운다(호스트 포트는 Docker가 배정, `docker compose port`). 공개 API 한 턴이 HTTP 추론 경계를 거치는지, 추론 컨테이너를 멈췄을 때 그 턴만 `NOT_COMMITTED`로 실패하고 서비스는 살아 있는지, 다시 띄우면 복구(404 → 재적재)하는지, 같은 `turn_id` 재시도가 처음 응답을 돌려주는지 확인한다. 실제 서빙 이미지가 나오면 compose의 `inference` 이미지만 바꾼다.
