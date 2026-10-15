#!/usr/bin/env bash
# 서비스 이미지와 추론 이미지(가짜 경로, 모델 없음)를 compose로 같이 띄워 공개 API 한 턴이 HTTP 추론 경계를 거쳐 도는지 확인한다.
# 추론 서버를 멈췄다가 다시 띄웠을 때의 복구까지 본다. 고정 포트 없음(docker compose port). 실패하면 0이 아닌 값으로 끝난다.
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=docker
if ! docker info >/dev/null 2>&1; then DOCKER=docker.exe; fi
PROJECT=openit-survey-service-compose-$$
export SERVICE_IMAGE=openit-survey-service:compose-$$
export FAKE_IMAGE=openit-survey-service-fake:compose-$$
COMPOSE="$DOCKER compose -p $PROJECT -f tests/compose/compose.smoke.yaml"
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
bash scripts/build_wheels.sh >/dev/null
# shellcheck disable=SC1091
source build/wheels/VERSIONS.env
$DOCKER build -q -t "$SERVICE_IMAGE" --build-arg COMMIT="$COMMIT" --build-arg BOUNDARY_TAG="$BOUNDARY_TAG" \
  --build-arg SCHEMA_CONTRACT_VERSION="$SCHEMA_CONTRACT_VERSION" --build-arg TURN_ENGINE_TAG="$TURN_ENGINE_TAG" \
  --build-arg MODEL_RUNTIME_TAG="$MODEL_RUNTIME_TAG" . >/dev/null
rm -rf build/fake-ctx && mkdir -p build/fake-ctx
cp -r build/wheels build/fake-ctx/wheels && cp requirements.lock build/fake-ctx/ && cp tests/compose/Dockerfile.fake build/fake-ctx/Dockerfile
mkdir build/fake-ctx/tests && (cd tests && cp *.py build/../../build/fake-ctx/tests/ 2>/dev/null || cp *.py ../build/fake-ctx/tests/)
rm -f build/fake-ctx/tests/test_*.py build/fake-ctx/tests/conftest.py
$DOCKER build -q -t "$FAKE_IMAGE" build/fake-ctx >/dev/null
cleanup() { $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true; $DOCKER rmi -f "$SERVICE_IMAGE" "$FAKE_IMAGE" >/dev/null 2>&1 || true; rm -f /tmp/smoke-compose-$$.body; }
trap cleanup EXIT
fail() { echo "SMOKE(compose) FAIL: $1"; $COMPOSE logs --tail 15 2>&1 | tail -40; exit 1; }

$COMPOSE up -d >/dev/null 2>&1 || fail "compose up"
PORT=$($COMPOSE port service 8080 | head -1 | tr -d '\r' | sed 's/.*://')
echo "host port: $PORT"
API="http://127.0.0.1:$PORT"
for i in $(seq 1 60); do curl -fsS "$API/readyz" >/dev/null 2>&1 && break; sleep 1; done
READY=$(curl -fsS "$API/readyz") || fail "readyz가 준비되지 않았다"
echo "readyz: $READY"
echo "$READY" | grep -q '"model_backend": "remote"' || fail "추론 경계(remote)를 쓰고 있지 않다"
echo "$READY" | grep -q '"active_release": "rel-compose"' || fail "활성 릴리스"
post() { curl -sS -o /tmp/smoke-compose-$$.body -w '%{http_code}' -X POST -H "Content-Type: application/json" -H "Authorization: Bearer smoke-api-token" -d "$2" "$API$1"; }
turn() { post /v1/sessions/cs-1/turns "{\"session_id\":\"cs-1\",\"kind\":\"UTTERANCE\",\"utterance\":\"$1\",\"turn_id\":\"$2\"}"; }
[ "$(post /v1/sessions '{"session_id":"cs-1","kind":"SESSION_START"}')" = 200 ] || fail "시작"
grep -q '"response_text": "(가짜 응답)"' /tmp/smoke-compose-$$.body || fail "추론 서버를 거친 응답 문장이 아니다: $(cat /tmp/smoke-compose-$$.body)"
[ "$(turn "안녕하세요" t-1)" = 200 ] && grep -q '"turn_failure_signal": null' /tmp/smoke-compose-$$.body || fail "발화 턴: $(cat /tmp/smoke-compose-$$.body)"
echo "turn: $(cat /tmp/smoke-compose-$$.body)"
curl -fsS "$API/readyz" | grep -q '"calls": [3-9]' || fail "추론 호출 통계가 없다"

# 추론 서버가 죽으면 그 턴만 실패(기록 안 됨)하고 서비스는 산다, 다시 뜨면 다음 턴부터 복구한다
$COMPOSE stop inference >/dev/null 2>&1
[ "$(turn "또 안녕" t-2)" = 200 ] && grep -q '"write_committed": "NOT_COMMITTED"' /tmp/smoke-compose-$$.body || fail "추론 서버가 없을 때 실패 신호가 아니다: $(cat /tmp/smoke-compose-$$.body)"
FAILED_BODY=$(cat /tmp/smoke-compose-$$.body)
curl -fsS "$API/healthz" >/dev/null || fail "추론 서버가 죽었다고 서비스가 죽었다"
$COMPOSE start inference >/dev/null 2>&1
for i in $(seq 1 30); do
  c=$(turn "다시 안녕" t-3); grep -q '"write_committed": "COMMITTED"' /tmp/smoke-compose-$$.body && [ "$c" = 200 ] && break; sleep 1
done
grep -q '"write_committed": "COMMITTED"' /tmp/smoke-compose-$$.body || fail "추론 서버가 다시 떠도 복구하지 않는다: $(cat /tmp/smoke-compose-$$.body)"
[ "$(post /v1/sessions/cs-1/end '{"session_id":"cs-1","kind":"SESSION_END"}')" = 200 ] || fail "종료"
# 같은 턴 ID 재시도는 같은 응답(이전 실패 응답 포함)
[ "$(turn "또 안녕" t-2)" = 200 ] && [ "$(cat /tmp/smoke-compose-$$.body)" = "$FAILED_BODY" ] || fail "같은 turn_id 재시도가 처음(실패) 응답과 다르다"
echo "SMOKE(compose) OK"
