#!/usr/bin/env bash
# 이미지를 빌드하고 컨테이너를 띄워 /healthz, /readyz, 비root, 라벨, 이미지 안의 한 턴(개발 하네스)을 확인한다.
# 포트는 고정하지 않는다(-p 127.0.0.1::8080 과 docker port). 실패하면 0이 아닌 값으로 끝난다.
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=docker
if ! docker info >/dev/null 2>&1; then DOCKER=docker.exe; fi
TESTS_DIR=$PWD/tests
if [ "$DOCKER" = docker.exe ] && command -v wslpath >/dev/null 2>&1; then TESTS_DIR=$(wslpath -w "$PWD/tests"); fi
IMG=openit-survey-service:smoke-$$
NAME=openit-survey-service-smoke-$$
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
bash scripts/build_wheels.sh >/dev/null
# shellcheck disable=SC1091
source build/wheels/VERSIONS.env
$DOCKER build -q -t "$IMG" --build-arg COMMIT="$COMMIT" --build-arg BOUNDARY_TAG="$BOUNDARY_TAG" \
  --build-arg SCHEMA_CONTRACT_VERSION="$SCHEMA_CONTRACT_VERSION" --build-arg TURN_ENGINE_TAG="$TURN_ENGINE_TAG" \
  --build-arg MODEL_RUNTIME_TAG="$MODEL_RUNTIME_TAG" . >/dev/null
trap '$DOCKER rm -f "$NAME" >/dev/null 2>&1 || true; $DOCKER rmi -f "$IMG" >/dev/null 2>&1 || true' EXIT

fail() { echo "SMOKE FAIL: $1"; $DOCKER logs "$NAME" 2>&1 | tail -20; exit 1; }

# 필수 설정이 없으면 시작하지 않는다(설정 오류로 끝)
code=$($DOCKER run --rm "$IMG" python -m survey_service >/dev/null 2>&1; echo $?) || true
[ "$code" = 2 ] || fail "설정이 없는데 시작했다 (종료 코드 $code)"

# 이웃(I4, I6)은 시험용 대역 팩토리로 꽂는다. 대역은 이미지에 없고 tests/ 를 읽기 전용으로 붙인다.
$DOCKER run -d --name "$NAME" -p 127.0.0.1::8080 -v "$TESTS_DIR:/mnt/tests:ro" -e PYTHONPATH=/mnt \
  -e SURVEY_RELEASE_READER=tests.smoke_fakes:reader -e SURVEY_TRACE_INGEST=tests.smoke_fakes:ingest \
  -e SURVEY_STAFF_CONSOLE=tests.smoke_fakes:console -e SURVEY_RESULT_DISPATCHER=tests.smoke_fakes:dispatcher \
  -e SURVEY_MODEL_BACKEND=fake -e SURVEY_RELEASE_POLL_SECONDS=1 "$IMG" >/dev/null
PORT=$($DOCKER port "$NAME" 8080/tcp | head -1 | tr -d '\r' | sed 's/.*://')
echo "host port: $PORT"
for i in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
echo "healthz: $(curl -fsS "http://127.0.0.1:$PORT/healthz")" || fail "healthz"
for i in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/readyz" >/dev/null 2>&1 && break
  sleep 1
done
READY=$(curl -fsS "http://127.0.0.1:$PORT/readyz") || fail "readyz가 준비되지 않았다"
echo "readyz:  $READY"
echo "$READY" | grep -q '"active_release": "rel-smoke"' || fail "활성 릴리스가 rel-smoke가 아니다"
echo "$READY" | grep -q '"contract_version": 1' || fail "계약 버전"

[ "$($DOCKER exec "$NAME" id -u | tr -d '\r')" != "0" ] || fail "root로 실행 중"
LABELS=$($DOCKER inspect --format '{{json .Config.Labels}}' "$IMG")
echo "labels:  $LABELS"
for want in "io.openit.contracts.boundary\":\"$BOUNDARY_TAG" "io.openit.turn-engine\":\"$TURN_ENGINE_TAG" \
            "io.openit.model-runtime\":\"$MODEL_RUNTIME_TAG" "io.openit.contracts.survey-schema\":\"$SCHEMA_CONTRACT_VERSION"; do
  echo "$LABELS" | grep -q "$want" || fail "라벨 누락: $want"
done
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -d '{}' "http://127.0.0.1:$PORT/v1/turns")
[ "$code" = 404 ] || fail "공개 API가 없어야 한다(O06 대기): $code"

# 이미지 안에서 실제 turn-engine, model-runtime(fake), 문진 스키마 패키지로 한 턴(개발 하네스)
$DOCKER exec "$NAME" python - <<'PY' || fail "이미지 안의 한 턴"
import json, tempfile, os, subprocess, sys
sys.path.insert(0, "/mnt")
from tests.sample_bank import sample_bank
d = tempfile.mkdtemp()
json.dump(sample_bank(), open(d + "/bank.json", "w"), ensure_ascii=False)
out = subprocess.run([sys.executable, "-m", "survey_service.cli", "--bank", d + "/bank.json", "--db", d + "/s.db", "turn", "--session", "S1", "--start"],
                     capture_output=True, text=True)
assert out.returncode == 0, out.stderr
r = json.loads(out.stdout)
assert r["orchestrator_output"]["log_entry"]["directive"]["action"] == "ASK_NEXT", r
print("in-image turn:", r["orchestrator_output"]["log_entry"]["directive"]["action"])
PY
# SIGTERM으로 제때 멈춘다
start=$(date +%s); $DOCKER stop -t 10 "$NAME" >/dev/null; el=$(( $(date +%s) - start ))
[ "$el" -le 8 ] || fail "정상 종료가 느리다: ${el}s"
echo "SMOKE OK"
