#!/usr/bin/env bash
# 이미지를 빌드하고 컨테이너를 띄워 /healthz, /readyz, 비root, 라벨, 이미지 안의 한 턴(개발 하네스)을 확인한다.
# 포트는 고정하지 않는다(-p 127.0.0.1::8080 과 docker port). 실패하면 0이 아닌 값으로 끝난다.
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=${DOCKER:-docker}   # 실행 파일 이름만 바꿀 수 있다(예: podman). 다른 우회는 두지 않는다
if ! "$DOCKER" info >/dev/null 2>&1; then
  echo "오류: 현재 사용자가 docker 데몬에 닿지 않는다. 데몬이 떠 있는지, 사용자에게 docker 권한이 있는지 확인하라." >&2
  exit 1
fi
TESTS_DIR=$PWD/tests
IMG=openit-survey-service:smoke-$$
NAME=openit-survey-service-smoke-$$
COMMIT=$(git rev-parse --short HEAD)      # 해시를 못 얻으면 실패한다(라벨 revision은 hex만 허용: ImageLabels)
WORK=$(mktemp -d)
REQ=$(bash scripts/dev-request.sh "$WORK" "$IMG")      # requirements.txt의 태그로 이웃 소스를 받아 BuildImageRequest를 만든다(운영은 deploy가 만든다)
RESULT=$(bash scripts/build-image.sh "$REQ" | tail -1)    # 영역 빌드 진입점(계약 2.5.0). 마지막 줄 = BuildImageResult
BOUNDARY_TAG=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["versions"]["boundary"])' "$REQ")
MODEL_RUNTIME_TAG=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["versions"]["model_runtime"])' "$REQ")
SCHEMA_CONTRACT_VERSION=$(tr -d '[:space:]' < schema/CONTRACT_VERSION)
LANGGRAPH_VERSION=$(sed -n 's/^langgraph==//p' requirements.lock | head -1)
echo "$RESULT" | python3 -c 'import json,sys;r=json.loads(sys.stdin.read());assert r["image_id"].startswith("sha256:") and r["labels"]["io.openit.service"]=="survey-service"' || { echo "SMOKE FAIL: 빌드 결과(BuildImageResult)가 올바르지 않다: $RESULT"; exit 1; }
trap '$DOCKER rm -f "$NAME" >/dev/null 2>&1 || true; $DOCKER rmi -f "$IMG" >/dev/null 2>&1 || true' EXIT

fail() { echo "SMOKE FAIL: $1"; $DOCKER logs "$NAME" 2>&1 | tail -20; exit 1; }

# 필수 설정이 없으면 시작하지 않는다(설정 오류로 끝)
code=$($DOCKER run --rm "$IMG" python -m survey_service >/dev/null 2>&1; echo $?) || true
[ "$code" = 2 ] || fail "설정이 없는데 시작했다 (종료 코드 $code)"

# 비밀은 파일로 받는다(D84): 직원 토큰은 NAME_FILE(읽기 전용 마운트), 키오스크 토큰은 개발용 환경 변수. 이미지 사용자(비root)가 읽을 수 있게 644
SECRET_DIR=$(mktemp -d)
printf 'smoke-staff-token\n' > "$SECRET_DIR/staff_token"; chmod 755 "$SECRET_DIR"; chmod 644 "$SECRET_DIR/staff_token"
trap '$DOCKER rm -f "$NAME" >/dev/null 2>&1 || true; $DOCKER rmi -f "$IMG" >/dev/null 2>&1 || true; rm -rf "$SECRET_DIR"' EXIT

# 이웃(I4, I6)은 시험용 대역 팩토리로 꽂는다. 대역은 이미지에 없고 tests/ 를 읽기 전용으로 붙인다.
$DOCKER run -d --name "$NAME" -p 127.0.0.1::8080 -v "$TESTS_DIR:/mnt/tests:ro" -v "$SECRET_DIR:/run/secrets:ro" -e PYTHONPATH=/mnt \
  -e SURVEY_RELEASE_READER=tests.smoke_fakes:reader -e SURVEY_TRACE_INGEST=tests.smoke_fakes:ingest \
  -e SURVEY_RESULT_DISPATCHER=tests.smoke_fakes:dispatcher -e SURVEY_RISK_SOURCE=tests.smoke_fakes:risk_source \
  -e SURVEY_API_TOKEN=smoke-api-token-0123 -e SURVEY_STAFF_API_TOKEN_FILE=/run/secrets/staff_token -e SURVEY_STATE_POLL_MIN_INTERVAL_SECONDS=0 -e SURVEY_SSE_HEARTBEAT_SECONDS=1 -e SURVEY_MODEL_BACKEND=fake -e SURVEY_RELEASE_POLL_SECONDS=1 "$IMG" >/dev/null
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
echo "$READY" | grep -q '"staff_alert_source": {"active": true}' || fail "스모크의 위험 입력원이 /readyz에 드러나지 않는다"
echo "$READY" | grep -q '"secrets": {"api_token": "env", "kiosk_device_tokens": "unset", "staff_api_token": "file"' || fail "비밀 출처(file|env)가 /readyz에 드러나지 않는다"
echo "$READY" | grep -q 'smoke-staff-token\|smoke-api-token-0123' && fail "/readyz에 비밀 값이 나왔다"
echo "$READY" | grep -q '"response_tables": {"status": "DATASET_RULES_UNCONFIRMED"' || fail "표 상태가 /readyz에 드러나지 않는다"

[ "$($DOCKER exec "$NAME" id -u | tr -d '\r')" != "0" ] || fail "root로 실행 중"
LABELS=$($DOCKER inspect --format '{{json .Config.Labels}}' "$IMG")
echo "labels:  $LABELS"
for want in "io.openit.service\":\"survey-service" "io.openit.revision\":\"$COMMIT" "io.openit.contracts.boundary\":\"$BOUNDARY_TAG" "io.openit.turn-engine\":\"n/a" \
            "io.openit.model-runtime\":\"$MODEL_RUNTIME_TAG" "io.openit.contracts.survey-schema\":\"$SCHEMA_CONTRACT_VERSION" "io.openit.langgraph\":\"$LANGGRAPH_VERSION"; do
  echo "$LABELS" | grep -q "$want" || fail "라벨 누락: $want"
done
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -d '{}' "http://127.0.0.1:$PORT/v1/turns")
[ "$code" = 404 ] || fail "공개 API 밖의 경로는 404여야 한다: $code"

# 공개 API 한 턴(PUBLIC_API.md): 시작, 발화(같은 turn_id 재시도는 같은 응답), 종료, 인증·404·409
API="http://127.0.0.1:$PORT"
post() { curl -sS -o /tmp/smoke-$$.body -w '%{http_code}' -X POST -H "Content-Type: application/json" -H "X-Openit-User: smoke-staff" ${3:+-H "Authorization: Bearer $3"} -d "$2" "$API$1"; }
[ "$(post /v1/sessions '{"session_id":"smoke-1","kind":"SESSION_START"}')" = 401 ] || fail "토큰 없는 시작이 거부되지 않음"
[ "$(post /v1/sessions '{"session_id":"smoke-1","kind":"SESSION_START"}' smoke-api-token-0123)" = 200 ] || fail "세션 시작"
grep -q '"response_text": "' /tmp/smoke-$$.body || fail "시작 응답에 문장이 없다: $(cat /tmp/smoke-$$.body)"
echo "api start: $(cat /tmp/smoke-$$.body)"
TURN='{"session_id":"smoke-1","kind":"UTTERANCE","utterance":"안녕하세요","turn_id":"t-1"}'
[ "$(post /v1/sessions/smoke-1/turns "$TURN" smoke-api-token-0123)" = 200 ] || fail "발화 턴"
A=$(cat /tmp/smoke-$$.body); echo "api turn:  $A"
[ "$(post /v1/sessions/smoke-1/turns "$TURN" smoke-api-token-0123)" = 200 ] && [ "$(cat /tmp/smoke-$$.body)" = "$A" ] || fail "같은 turn_id가 같은 응답이 아니다"
[ "$(post /v1/sessions/nope/turns '{"session_id":"nope","kind":"UTTERANCE","utterance":"x","turn_id":"t-1"}' smoke-api-token-0123)" = 404 ] || fail "모르는 세션이 404가 아니다"
[ "$(post /v1/sessions/smoke-1/end '{"session_id":"smoke-1","kind":"SESSION_END"}' smoke-api-token-0123)" = 200 ] || fail "세션 종료"
[ "$(post /v1/sessions/smoke-1/turns '{"session_id":"smoke-1","kind":"UTTERANCE","utterance":"x","turn_id":"t-2"}' smoke-api-token-0123)" = 409 ] || fail "끝난 세션이 409가 아니다"
# 위험으로 멈춘 세션을 직원이 풀어 이어가는 한 바퀴(스모크 전용 위험 입력원 tests.smoke_fakes:risk_source가 잡는다. 실제 입력원은 아직 없다, CR-012), 직원이 끝내는 경우, 역할 분리
rk() { post "/v1/sessions/$1/turns" "{\"session_id\":\"$1\",\"kind\":\"UTTERANCE\",\"utterance\":\"$2\",\"turn_id\":\"$3\"}" smoke-api-token-0123; }
iv() { post "/v1/sessions/$1/intervention" "{\"session_id\":\"$1\",\"decision\":\"$2\",\"turn_id\":\"$3\"}" "$4"; }
[ "$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "Content-Type: application/json" -H "Authorization: Bearer smoke-staff-token" -d '{"session_id":"smoke-1","decision":"RESUME","turn_id":"i-x"}' "$API/v1/sessions/smoke-1/intervention")" = 401 ] || fail "X-Openit-User 없는 개입이 401이 아니다"
# 전달 경로가 없을 때(SSE 연결도 푸시도 없다): 알림은 저장되지만 DELIVERY_FAILED -> 목록에서는 조회된다
[ "$(post /v1/sessions '{"session_id":"smoke-3","kind":"SESSION_START"}' smoke-api-token-0123)" = 200 ] || fail "세션 3 시작"
[ "$(rk smoke-3 "가슴이 너무 아파" r-1)" = 200 ] || fail "위험 발화 턴 3"
grep -q '"staff_alert": "DELIVERY_FAILED"' /tmp/smoke-$$.body || fail "전달 경로가 없는데 DELIVERY_FAILED가 아니다: $(cat /tmp/smoke-$$.body)"
curl -s -H "Authorization: Bearer smoke-staff-token" "$API/v1/staff/alerts?status=OPEN" | grep -q '"session_id": "smoke-3"' || fail "DELIVERY_FAILED여도 알림은 조회되어야 한다"
# 직원 알림: SSE를 연결해 둔 채 위험 발화(전달 경로 있음 -> DELIVERED), 목록·해결·푸시 구독·세션 상태
curl -sN -H "Authorization: Bearer smoke-staff-token" "$API/v1/staff/alerts/stream" > /tmp/smoke-sse-$$.txt 2>/dev/null &
SSE_PID=$!
sleep 1
curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer smoke-api-token-0123" "$API/v1/staff/alerts" | grep -q 403 || fail "키오스크 토큰으로 알림 목록이 열린다"
curl -s -H "Authorization: Bearer smoke-staff-token" "$API/v1/staff/push-config" | grep -q '"vapid_public_key": null' || fail "VAPID 키가 없으면 푸시 설정은 null이어야 한다"
SUB='{"endpoint":"https://push.example/smoke","keys":{"p256dh":"k","auth":"a"}}'
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer smoke-staff-token" -d "$SUB" "$API/v1/staff/push-subscriptions")" = 201 ] || fail "푸시 구독 등록"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer smoke-staff-token" -d "$SUB" "$API/v1/staff/push-subscriptions")" = 201 ] || fail "같은 구독 재등록(멱등)"
[ "$(post /v1/sessions '{"session_id":"smoke-2","kind":"SESSION_START"}' smoke-api-token-0123)" = 200 ] || fail "세션 2 시작"
[ "$(rk smoke-2 "숨이 안 쉬어져요" r-1)" = 200 ] || fail "위험 발화 턴"
grep -q '"response_text": "' /tmp/smoke-$$.body || fail "위험 안내 문장이 없다"
grep -q '"staff_alert": "DELIVERED"' /tmp/smoke-$$.body || fail "SSE가 연결돼 있는데 DELIVERED가 아니다: $(cat /tmp/smoke-$$.body)"
sleep 1; grep -q '^event: alert' /tmp/smoke-sse-$$.txt && grep -q '"kind": "RISK_STOP"' /tmp/smoke-sse-$$.txt || fail "SSE로 알림이 오지 않았다: $(cat /tmp/smoke-sse-$$.txt)"
! grep -q "숨이" /tmp/smoke-sse-$$.txt || fail "알림에 환자 발화가 들어 있다"
curl -s -H "Authorization: Bearer smoke-staff-token" "$API/v1/staff/alerts?status=OPEN" | grep -q '"session_id": "smoke-2"' || fail "열린 알림 목록에 없다"
curl -s -H "Authorization: Bearer smoke-api-token-0123" "$API/v1/sessions/smoke-2/state" | grep -q '"state": "PAUSED_RISK"' || fail "세션 상태가 PAUSED_RISK가 아니다"
[ "$(rk smoke-2 "언제 와요" r-2)" = 200 ] && grep -q '"response_text": null' /tmp/smoke-$$.body || fail "멈춘 세션은 할 말이 없어야 한다: $(cat /tmp/smoke-$$.body)"
[ "$(iv smoke-2 RESUME i-0 smoke-api-token-0123)" = 403 ] || fail "키오스크 토큰으로 개입이 허용됐다"
[ "$(iv smoke-2 RESUME i-0 wrong)" = 401 ] || fail "틀린 토큰이 401이 아니다"
[ "$(iv smoke-2 RESUME i-1 smoke-staff-token)" = 200 ] || fail "직원 재개"
R=$(cat /tmp/smoke-$$.body); echo "api resume: $R"; echo "$R" | grep -q '"response_text": "' || fail "재개 응답에 문장이 없다"
sleep 1; grep -q '^event: resolved' /tmp/smoke-sse-$$.txt || fail "해결 이벤트가 SSE로 오지 않았다"
curl -s -H "Authorization: Bearer smoke-staff-token" "$API/v1/staff/alerts?status=RESOLVED" | grep -q '"resolved_by_decision": "RESUME"' || fail "알림이 RESOLVED가 아니다"
curl -s -H "Authorization: Bearer smoke-api-token-0123" "$API/v1/sessions/smoke-2/state" | grep -q '"state": "ACTIVE", "resume_text": "' || fail "재개 뒤 상태가 resume_text와 함께 ACTIVE가 아니다"
[ "$(iv smoke-2 RESUME i-1 smoke-staff-token)" = 200 ] && [ "$(cat /tmp/smoke-$$.body)" = "$R" ] || fail "같은 turn_id의 개입이 같은 응답이 아니다"
[ "$(iv smoke-2 RESUME i-2 smoke-staff-token)" = 409 ] || fail "멈추지 않은 세션의 개입이 409가 아니다"
[ "$(rk smoke-2 "안녕하세요" r-3)" = 200 ] || fail "재개 뒤 발화가 이어지지 않는다"
curl -s -H "Authorization: Bearer smoke-api-token-0123" "$API/v1/sessions/smoke-2/state" | grep -q '"resume_text": null' || fail "발화가 접수된 뒤 resume_text가 null이 아니다"
# 연결 전에 쌓인 알림은 Last-Event-ID로 이어 받는다
kill $SSE_PID 2>/dev/null; wait $SSE_PID 2>/dev/null || true
curl -sN -m 3 -H "Authorization: Bearer smoke-staff-token" -H "Last-Event-ID: 0" "$API/v1/staff/alerts/stream" > /tmp/smoke-sse2-$$.txt 2>/dev/null || true
grep -q '"session_id": "smoke-3"' /tmp/smoke-sse2-$$.txt || fail "Last-Event-ID로 놓친 알림을 이어 받지 못했다"
[ "$(iv smoke-3 END i-1 smoke-staff-token)" = 200 ] || fail "직원 종료"
[ "$(rk smoke-3 "x" r-2)" = 409 ] || fail "직원이 끝낸 세션이 409가 아니다"
rm -f /tmp/smoke-$$.body /tmp/smoke-sse-$$.txt /tmp/smoke-sse2-$$.txt
# 이미지 안에서 model-runtime(fake), 문진 스키마 패키지로 한 턴(개발 하네스)
$DOCKER exec -e EXPECT_LANGGRAPH="$LANGGRAPH_VERSION" "$NAME" python - <<'PY' || fail "이미지 안의 한 턴"
import json, tempfile, os, subprocess, sys
sys.path.insert(0, "/mnt")
from tests.sample_bank import sample_bank
d = tempfile.mkdtemp()
json.dump(sample_bank(), open(d + "/bank.json", "w"), ensure_ascii=False)
out = subprocess.run([sys.executable, "-m", "survey_service.cli", "--bank", d + "/bank.json", "--db", d + "/s.db", "turn", "--session", "S1", "--start"],
                     capture_output=True, text=True)
assert out.returncode == 0, out.stderr
r = json.loads(out.stdout)
assert r["frontend_input"]["response_text"] == "(가짜 응답)" and r["orchestrator_output"]["log_entry"]["turn_result"] == "COMMITTED", r
import importlib.metadata as md, os
assert md.version("langgraph") == os.environ.get("EXPECT_LANGGRAPH", md.version("langgraph")), "langgraph 버전이 잠금과 다르다"
assert os.environ.get("LANGSMITH_TRACING") == "false" and os.environ.get("LANGCHAIN_TRACING_V2") == "false", "외부 추적이 꺼져 있지 않다"
print("in-image turn:", r["frontend_input"]["response_text"])
PY
# 재시작해도 알림이 남아 있다(영속 저장)
$DOCKER restart -t 10 "$NAME" >/dev/null
PORT=$($DOCKER port "$NAME" 8080/tcp | head -1 | tr -d '\r' | sed 's/.*://'); API="http://127.0.0.1:$PORT"
for i in $(seq 1 30); do curl -fsS "$API/readyz" >/dev/null 2>&1 && break; sleep 1; done
curl -s -H "Authorization: Bearer smoke-staff-token" "$API/v1/staff/alerts?status=RESOLVED" | grep -q '"session_id": "smoke-3"' || fail "재시작 뒤 알림 목록이 비었다"
curl -s -H "Authorization: Bearer smoke-staff-token" "$API/v1/staff/alerts?status=RESOLVED" | grep -q '"session_id": "smoke-2"' || fail "재시작 뒤 해결된 알림이 없다"
# SIGTERM으로 제때 멈춘다
start=$(date +%s); $DOCKER stop -t 10 "$NAME" >/dev/null; el=$(( $(date +%s) - start ))
[ "$el" -le 8 ] || fail "정상 종료가 느리다: ${el}s"

# 선택(SMOKE_PG=1): 같은 이미지를 PostgreSQL(SURVEY_DATABASE_URL)로 띄워 한 바퀴. 개발용 서버는 이 리포의 compose.dev.yaml(일회용, 메모리 데이터)로 띄운다. 이웃 리포(deploy)의 스크립트를 쓰지 않는다.
if [ "${SMOKE_PG:-0}" = 1 ]; then
  PROJ="openit-survey-service-smokepg-$$"      # 프로젝트 이름에 리포 이름을 넣어 다른 영역과 겹치지 않게 한다
  DC() { $DOCKER compose -p "$PROJ" -f compose.dev.yaml "$@"; }
  trap '$DOCKER rm -f "$NAME" "$NAME-pg" >/dev/null 2>&1 || true; $DOCKER rmi -f "$IMG" >/dev/null 2>&1 || true; rm -rf "$SECRET_DIR"; DC down -v >/dev/null 2>&1 || true' EXIT
  DC up -d --wait >/dev/null 2>&1 || fail "compose.dev.yaml의 PostgreSQL을 띄우지 못했다"
  # 앱 컨테이너는 compose가 만든 네트워크에 붙어 서비스 이름(postgres)으로 접속한다(호스트 루프백 포트는 컨테이너에서 보이지 않는다)
  PGNET="${PROJ}_default"
  printf 'postgresql://postgres@postgres:5432/survey\n' > "$SECRET_DIR/db_url"; chmod 644 "$SECRET_DIR/db_url"
  PGPORT_APP=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')   # 빈 포트(고정하지 않는다)
  runpg() { $DOCKER run -d --name "$NAME-pg" --network "$PGNET" -p 127.0.0.1:"$PGPORT_APP":8080 -v "$TESTS_DIR:/mnt/tests:ro" -v "$SECRET_DIR:/run/secrets:ro" -e PYTHONPATH=/mnt \
    -e SURVEY_DATABASE_URL_FILE=/run/secrets/db_url -e SURVEY_RELEASE_READER=tests.smoke_fakes:reader \
    -e SURVEY_TRACE_INGEST=tests.smoke_fakes:ingest -e SURVEY_API_TOKEN=smoke-api-token-0123 -e SURVEY_STAFF_API_TOKEN_FILE=/run/secrets/staff_token \
    -e SURVEY_MODEL_BACKEND=fake -e SURVEY_RELEASE_POLL_SECONDS=1 "$IMG" >/dev/null; }
  runpg
  PAPI="http://127.0.0.1:$PGPORT_APP"
  for i in $(seq 1 40); do curl -fsS "$PAPI/readyz" >/dev/null 2>&1 && break; sleep 1; done
  PREADY=$(curl -fsS "$PAPI/readyz") || { $DOCKER logs "$NAME-pg" 2>&1 | tail -5; fail "PostgreSQL 모드의 readyz가 준비되지 않았다"; }
  echo "$PREADY" | grep -q '"database": {"kind": "postgresql"}' || fail "/readyz의 database.kind가 postgresql이 아니다"
  [ "$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H 'Authorization: Bearer smoke-api-token-0123' -d '{"session_id":"pg-1","kind":"SESSION_START"}' "$PAPI/v1/sessions")" = 200 ] || fail "PostgreSQL 모드의 세션 시작"
  $DOCKER restart -t 10 "$NAME-pg" >/dev/null
  for i in $(seq 1 40); do curl -fsS "$PAPI/readyz" >/dev/null 2>&1 && break; sleep 1; done
  curl -fsS -H 'Authorization: Bearer smoke-api-token-0123' "$PAPI/v1/sessions/pg-1/state" | grep -q '"state": "ACTIVE"' || fail "재시작 뒤 PostgreSQL의 세션이 남아 있지 않다"
  { echo "$PREADY"; $DOCKER logs "$NAME-pg" 2>&1; } | grep -q "postgres@postgres" && fail "접속 문자열이 /readyz나 로그에 나왔다"
  echo "pg smoke: database.kind=postgresql, 재시작 뒤 세션 유지, 접속 문자열 비노출"
fi
echo "SMOKE OK"
