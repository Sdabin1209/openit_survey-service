# CR-010 추론 서버 규약: model-runtime 영역이 구현할 HTTP 서빙 서버

- 요청: 헤드 → model-runtime 영역 (survey-service가 규약 초안을 썼다). 상태: 확인 요청.
- 내용: `docs/inference-server-spec.md`. 서비스는 `SURVEY_INFERENCE_URL`로 HTTP 호출만 한다. 서버는 `PUT /v1/releases/{id}`(적재 신호, 멱등, 200/202), `GET /v1/releases/{id}`(상태), `POST /v1/releases/{id}/parse|generate`(계약 그대로), `GET /v1/info`, `/healthz`, `/readyz`를 낸다. 릴리스는 서버가 스스로 I4(HttpReleaseReader)에서 읽어 적재한다.
- survey-service 쪽에서 정리한 것: 서비스 안의 추론 서버(`survey_service/inference_server.py`)는 서비스 패키지에서 뺐다. 참조 구현은 시험 대역(`tests/ref_inference_server.py`, model-runtime의 FakeModelSession 경로, 모델 없음)이고 compose 스모크의 가짜 추론 컨테이너로만 쓴다. 책임 겹침 없음.
- model-runtime이 정해 줄 것: (1) 위 규약의 수락 또는 수정(특히 202 적재 폴링, 404 재적재, 503 대기열 거절), (2) `X-Openit-Outcome`·`X-Openit-Attempts` 응답 헤더 제공 여부, (3) 한 서버에 릴리스 여러 개를 동시에 올릴 수 있는지, (4) 서버 이미지의 환경 변수·포트·라벨.
- 맞추는 방법: 실제 서버 이미지가 나오면 `tests/compose/compose.smoke.yaml`의 `inference` 서비스 이미지를 바꿔 `scripts/smoke_compose.sh`를 돌리고, `tests/test_inference_client.py::test_spec_endpoints_over_raw_http`의 URL을 실제 서버로 향하게 해 같은 확인을 한다.
