# 변경 이력

## 0.1.0 (미출시, 2026-10-15)

- 문진 스키마 패키지 `openit-survey-schema` 1.0.0 (`schema/`): 정수 버전별 디렉터리, `load_schema(name, version=1)`, `schema_dir(version=1)`. `CONTRACT_VERSION`(1)과 스키마 파일은 바꾸지 않았다.
- survey-service 0.1.0: Orchestrator(처리 표 9-1, 쓰기 확정, 확정 뒤의 일, 실패 신호, 턴 멱등), Session Store(SQLite·메모리, 반영 효과 9-2), 릴리스 로더(승인·해시·계약 버전 검사, 거부 시 이전 유지, 새 세션부터 적용), 추론 서버(fake 백엔드), Trace outbox와 내보내기(최소 1회, UNAVAILABLE만 재시도), `/healthz` `/readyz`, 개발용 CLI.
- 의존: boundary-contracts v1.1.0, turn-engine v0.2.0, model-runtime v0.3.0(역할별 serving_config).
- 컨테이너: Dockerfile, `scripts/build_wheels.sh`, `scripts/smoke.sh`.
- 골든 대조: 226 케이스, 비교 1220쌍 중 1215 일치, 5건은 예시 오류(CR-008).
- 헤드 결정 반영: bindings.yaml 대조(`tests/test_bindings.py`), 라이브러리 MAJOR.MINOR 불일치 거부, 외부 ID `x-` 변환, 역할별 serving_config 처리 분리.
- 변경 요청: CR-001(스키마 변경 후보, 적용 안 함) ~ CR-008.
