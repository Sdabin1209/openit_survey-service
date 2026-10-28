# 추론 서버 규약 (survey-service <-> 모델 서빙 컨테이너)

> **[D74로 대체됨]** 호출은 이제 `extract`(ActionExtractionInput -> ActionExtraction, JSON)와 `respond`(ResponseRequest -> 응답 문장, text/plain)다. 아래 `parse`/`generate`, `UtteranceParserInput`, `ParserOutput`, `ResponseGeneratorInput`, `GeneratorOutput`, `is_fallback` JSON 본문은 v1이다. 기준은 `boundary-contracts/HTTP.md`의 I9 절(2.0.0)이고, 서비스 쪽 실패 처리는 design 16절과 `survey_service/remote.py`의 머리말이다: 503 `OUTPUT_INVALID`와 검증 위반은 같은 요청을 1회 재시도하고 그래도면 턴 실패(D75), 연결 실패는 클라이언트 층에서 1회 재시도, 읽기 타임아웃·400은 재시도 없음, 404는 릴리스를 한 번 다시 적재. 이 문서의 나머지는 v1 시절 기록이다.

> **기준은 `boundary-contracts/HTTP.md`의 'I9 ModelRuntime 추론 서버' 절이다(D62).** 이 문서는 그 절의 근거와 서비스 쪽 실패 처리(5절)를 담고, 어긋나면 HTTP.md에 맞춘다. 보호 헤더는 `X-Openit-Outcome`, `X-Openit-Attempts`, `X-Openit-Protections`(규칙 코드:건수, 예 `P1:1,G5:1`)다. `/v1/info`에는 `serving_config_hash`(역할별)가 더해졌다.

상태: survey-service가 정한 제안 규약 v1(헤드 확인 요청, CR-010). 구현은 **model-runtime 영역이 만드는 추론 서버 이미지**(vLLM, 서빙 서버 RTX 4090)이고, 서비스 쪽은 `survey_service/remote.py`의 HTTP 클라이언트뿐이다. 서비스는 모델을 직접 적재하거나 호출하지 않는다(`SURVEY_INFERENCE_URL`이 있으면). 참조 구현(모델 없는 가짜 경로)과 클라이언트 시험은 `tests/ref_inference_server.py`, `tests/test_inference_client.py`에 있고, 실제 서버는 같은 시험의 HTTP 부분(`test_spec_endpoints_over_raw_http`)과 5절의 동작을 만족해야 한다.

## 1. 책임

| | 서비스(survey-service) | 추론 서버(model-runtime 영역) |
|---|---|---|
| 릴리스 | I4에서 포인터를 보고 릴리스를 결정한다. 질문지·상태를 쓴다. 릴리스 적재 신호를 보낸다 | 신호를 받은 릴리스의 어댑터와 설정을 **스스로 I4에서 읽어**(HttpReleaseReader, D49) 적재한다. 서비스는 어댑터 바이트를 보내지 않는다 |
| 검사 | 승인(PASS)·해시·계약 버전·라이브러리 MAJOR.MINOR를 검사한다 | 같은 검사를 한다(model-runtime `load`: 설정 해시, 어댑터 사전 검사, 계약 버전) |
| 출력 | 응답이 출력 계약을 지키는지 확인만 한다. 대체 출력을 만들지 않는다 | 출력 보호(D36)를 한다. 항상 출력 계약을 통과하는 값을 낸다 |
| 장애 | 턴 단계 실패로 기록한다(재시도 규칙 5절) | 일시 불능은 503, 입력 위반은 400으로 구분해 알린다 |

## 2. 공통

- HTTP/JSON(UTF-8). `Authorization: Bearer <토큰>`(서비스는 `SURVEY_INFERENCE_TOKEN_ENV`가 가리키는 환경 변수에서 읽는다). `GET /healthz`, `GET /readyz`(인증 없음; readyz는 엔진이 요청을 받을 수 있을 때 200).
- 오류는 `BoundaryError` 레코드 본문(`code`, `message`, `retryable`, 선택 `details`)과 HTTP.md 9절의 상태 코드(400 INVALID_RECORD, 403 PERMISSION_DENIED, 404 NOT_FOUND, 412 FAILED_PRECONDITION, 422 INTEGRITY_ERROR·UNSUPPORTED_VERSION, 503 UNAVAILABLE). `details`에 모델 런타임 예외 이름과 역할(예: `{"reason": "ServingConfigMismatch", "role": "LORA_A"}`)을 담을 수 있다. 발화·모델 출력 원문은 본문·로그에 넣지 않는다.
- 요청·응답 본문은 문진 스키마의 계약 그대로다(`UtteranceParserInput`, `ParserOutput`, `ResponseGeneratorInput`, `GeneratorOutput`). 서비스가 쓰는 정수 버전은 `/v1/info`의 `supported_contract_versions`에 있어야 한다.

## 3. 엔드포인트

| 요청 | 응답 |
|---|---|
| `PUT /v1/releases/{release_id}` 릴리스 적재 신호(본문 없음). **멱등** | `200 {"release_id", "state": "LOADED", "error": null}` 이미 적재됨/방금 끝남. `202 {"state": "LOADING"}` 적재 중(오래 걸리면). 실패: `412`(승인 없음·해시·설정 불일치·어댑터 사전 검사 실패), `422`(지원하지 않는 계약 버전), `503`(I4 불능·GPU 부족, 재시도 가능) |
| `GET /v1/releases/{release_id}` 적재 상태 | `200 {"release_id", "state": "ABSENT"\|"LOADING"\|"LOADED"\|"FAILED", "error": BoundaryError\|null}` |
| `POST /v1/releases/{release_id}/parse` 본문 `UtteranceParserInput` | `200 ParserOutput` |
| `POST /v1/releases/{release_id}/generate` 본문 `ResponseGeneratorInput` | `200 GeneratorOutput` |
| `GET /v1/info` (선택, 점검용) | `200 {"service", "runtime_version", "supported_contract_versions": [1], "backend", "loaded": [release_id...]}` |

- parse·generate 응답 헤더: `X-Openit-Outcome: ok|repaired|fallback`(출력 보호 결과, `last_call_info.outcome`), `X-Openit-Attempts: <시도 횟수>`, `X-Openit-Protections: P1:1,G5:1`(규칙 코드별 건수). 서비스는 값을 세기만 하고 `/readyz`의 `inference`에 `outcome_*`와 규칙별 `protections`로 낸다(원문 없음).
- 적재되지 않은 릴리스에 parse/generate가 오면 **404 NOT_FOUND**(서버가 재시작해 잊은 경우 포함). 서비스는 한 번 `PUT`으로 적재를 요청하고, 200 LOADED면 호출을 다시 하고 202면 그 턴을 실패시킨다(기다리지 않는다).
- 적재 신호는 서비스가 새 릴리스를 적재할 때(`PUT`, 202면 `GET`을 0.5초 간격으로 `SURVEY_INFERENCE_LOAD_TIMEOUT_SECONDS`(기본 600초)까지 확인)와 진행 중 세션이 쓰는 릴리스를 다시 올릴 때 보낸다. 같은 서버에 서로 다른 릴리스 여러 개를 동시에 올려 둘 수 있어야 한다(진행 중 세션은 자기 릴리스로 끝난다).
- 서로 다른 세션의 호출은 동시에 들어온다. 서버는 병렬로 처리하거나 짧은 대기열 뒤에서 처리하되, 대기가 호출 타임아웃(기본 4초)을 넘을 것 같으면 **큐에 쌓지 말고 503**으로 거절한다(재시도 가능).
- 추론 호출은 부작용이 없다(서버는 상태를 바꾸지 않는다). 같은 요청이 다른 출력을 낼 수 있다(디코딩 시드). 서비스는 재현성에 기대지 않는다.

## 4. 서비스 쪽 설정

`SURVEY_INFERENCE_URL`(base URL), `SURVEY_INFERENCE_TOKEN_ENV`, `SURVEY_INFERENCE_EXTRACT_TIMEOUT_SECONDS`(기본 3.0), `SURVEY_INFERENCE_RESPOND_TIMEOUT_SECONDS`(기본 2.0), `SURVEY_INFERENCE_TIMEOUT_SECONDS`(제어 호출용, 기본 4), `SURVEY_INFERENCE_RETRIES`(기본 1), `SURVEY_INFERENCE_LOAD_TIMEOUT_SECONDS`(기본 600). 호출별 상한은 서버 턴 마감 6초 안에 extract와 respond 두 호출이 들어가도록 계약 표(2.2.0)대로 3.0+2.0으로 정했다. 재시도는 남은 턴 예산이 호출 상한 이상일 때만 한다(ADR-0026, 측정하지 않은 배분값).

## 5. 서비스의 실패 처리 (`tests/test_inference_client.py`가 느린 서버·죽은 서버·틀린 출력까지 확인한다)

| 상황 | 서비스의 처리 | 턴 결과 |
|---|---|---|
| 정상 | 출력 계약을 검사해 쓴다. `X-Openit-Outcome`이 repaired·fallback이면 건수를 센다 | 정상 (보호가 적용된 출력도 정상 출력이다) |
| 연결 실패, 502/503/504 | 짧은 간격(0.2초)으로 `RETRIES`번까지 다시 시도한다 | 계속 실패하면 해당 단계 실패 |
| **읽기 타임아웃**(느린 응답) | 다시 시도하지 않는다(서버가 바쁘다는 뜻이고 지연만 두 배가 된다). 타임아웃 건수를 센다 | 해당 단계 실패 |
| 400(입력이 계약을 어김) | 결정적이라 다시 시도하지 않는다 | 해당 단계 실패 |
| 404(릴리스를 잊음) | `PUT`으로 다시 적재를 요청한다: 200이면 호출을 다시 하고, 202(적재 중)면 기다리지 않고 | 202면 단계 실패, 다음 턴부터 복구 |
| 200인데 본문이 JSON이 아니거나 출력 계약을 어김 | 서버 결함으로 본다. 대체 출력을 만들지 않고 다시 시도하지 않는다 | 해당 단계 실패 |
| 토큰 오류(401/403) | 다시 시도하지 않는다 | 해당 단계 실패 |
| 적재 신호 실패/거부/시간 초과 | 릴리스 적재를 거부하고 **이전 릴리스를 유지**한다(`REMOTE_<코드>`, `/readyz`의 `release_loader.rejections`) | 새 세션은 이전 릴리스 |

단계 실패의 의미(규칙 J7 계열): parse 실패는 `BEFORE_COMMIT/LoRA-A`로 쓰기 없이 `NOT_COMMITTED` 신호, generate 실패는 `AFTER_COMMIT/LoRA-B`로 쓰기는 반영되고 응답 문장만 없다(`COMMITTED` 신호). 서비스는 추론 서버가 죽어도 죽지 않고 `/healthz`가 산다. 추론 서버가 다시 뜨면 다음 호출에서 404 → 재적재로 복구한다.

## 6. 열린 것

- 서버 내부의 대기열·배치 정책, 한 GPU에서 릴리스 여러 개를 올릴 때의 메모리 정책(model-runtime). 기반 모델이 다른 두 어댑터(미정, 백엔드 구현 때).
- TLS와 토큰 회전(배포). 서비스 쪽 로더가 번들의 어댑터까지 받는 낭비(I4에 질문지만 받는 호출이 생기면 줄인다).
