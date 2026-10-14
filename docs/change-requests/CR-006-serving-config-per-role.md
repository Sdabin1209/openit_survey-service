# CR-006 ReleaseManifest의 역할별 serving_config와 I9 load의 단일 serving_config

- 상태: 확인 요청. 현재는 가장 보수적으로 처리했다.
- 사실: `ReleaseManifest.adapters`는 LORA_A와 LORA_B가 각자 `serving_config`와 `serving_config_hash`를 가진다. 그런데 I9 `load(adapters, serving_config, *, schema_dir, expected_serving_config_hash, backend)`는 두 어댑터를 한 번에 받고 `serving_config` 하나만 받는다(model-runtime 0.2.0에서 어댑터가 하나만 있으면 `ContractViolation`).
- 현재 동작: 두 역할의 `serving_config`·해시가 같을 때만 적재한다. 다르면 `SERVING_CONFIG_DIFFERS_BY_ROLE`로 거부하고 이전 릴리스를 유지한다(`/readyz`에 사유).
- 질문: (a) 항상 같다고 계약에 적고 `ReleaseManifest`에서 하나로 줄이는지, (b) 역할별로 다를 수 있다면 I9 `load`가 역할별 설정을 받게 바꾸는지(model-runtime), (c) 지금처럼 거부로 두는지.
- 영향 영역: release-gate(매니페스트), model-lab(패키지 매니페스트), model-runtime(load), survey-service. 계약 버전: `ReleaseManifest`는 경계 계약(MAJOR.MINOR)이라 (a)는 MAJOR일 수 있다.
