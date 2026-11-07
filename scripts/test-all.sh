#!/usr/bin/env bash
# 영역 전체 시험: compose.dev.yaml 의 일회용 PostgreSQL(호스트 포트는 Docker 배정, 데이터는 tmpfs)을 실행마다 고유한 프로젝트로 띄워
# TEST_DATABASE_URL 로 넘기고 pytest 전체를 돌린 뒤 내린다. 이미 TEST_DATABASE_URL 이 있으면 그대로 쓴다(시험 안에서 다시 부를 때 바깥 DB 를 내리지 않게).
# 왜: TEST_DATABASE_URL 이 없으면 PostgreSQL 시험이 건너뛰어지는데(tests/pg_support.py), 환경 보고의 test_command 가 pytest 만 가리켜
# deploy 사전 점검이 PostgreSQL 시험을 돌리지 않았다(2026-11-07). 이 명령이 환경 보고(SetupEnvReport.test_command)의 정본이다. 종료 코드는 pytest 의 것.
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/docker-env.sh   # DOCKER_CONFIG·DOCKER_HOST 규칙(사용자 ~/.docker 에 기대지 않는다)
PY=${PYTHON:-.venv/bin/python}
DOCKER=${DOCKER:-docker}
if [ -z "${TEST_DATABASE_URL:-}" ]; then
  PROJ="openit-survey-service-testpg-$$-$(date +%s)"
  dc() { $DOCKER compose -p "$PROJ" -f compose.dev.yaml "$@"; }
  trap 'dc down -v >/dev/null 2>&1 || true' EXIT
  dc up -d --wait >&2 || { echo "오류: 시험용 PostgreSQL 을 띄우지 못했다" >&2; exit 2; }
  PORT=$(dc port postgres 5432 | grep '^127\.0\.0\.1:' | head -1 | cut -d: -f2)
  [ -n "$PORT" ] || { echo "오류: 시험용 PostgreSQL 의 배정 포트를 읽지 못했다" >&2; exit 2; }
  export TEST_DATABASE_URL="postgresql://postgres@127.0.0.1:$PORT/survey"
  # 공식 이미지는 초기화 동안 소켓만 여는 임시 서버를 먼저 띄운다: TCP 로 실제 접속될 때까지 기다린다(상한 120회 x 0.5초, ops-feedback dev-postgres 와 같은 근거)
  for _ in $(seq 1 120); do "$PY" -c 'import os,psycopg; psycopg.connect(os.environ["TEST_DATABASE_URL"], connect_timeout=2).close()' >/dev/null 2>&1 && break; sleep 0.5; done
fi
# 외부 실제 데이터가 있어야 하는 선택 대조(표지 reference_data)는 명시적으로 뺀다: 결과에 'deselected' 로 드러난다(건너뛰기로 숨기지 않는다)
"$PY" -m pytest -p no:cacheprovider -m "not reference_data" "$@"   # -q 는 pyproject addopts 에 있다(두 번 주면 요약 줄이 사라져 사전 점검이 통과 개수를 못 센다)
