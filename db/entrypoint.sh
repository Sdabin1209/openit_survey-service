#!/bin/bash
# survey-db 진입점(postgres 사용자 uid 999로 시작, 공통 규칙 'root가 아닌 사용자'): TLS 인증서를 서버가 쓸 사본으로 복사하고 공식 entrypoint로 넘긴다. 서버 TLS 필수(AreaDatabaseEnv.DB_TLS_DIR의 tls.crt·tls.key).
# 왜 복사하나: PostgreSQL은 개인 키가 그룹·모두에게 읽히면(0640 초과, 소유자가 root가 아니면 0600 초과) 서버 시작을 거부한다. 읽기 전용 마운트는 권한을 바꿀 수 없다.
# 인증서는 24시간마다 새로 발급된다(step-ca): 60초마다 원본이 바뀌었는지 보고 바뀌면 복사 뒤 reload한다(재시작 없음).
set -euo pipefail
TLS_SRC=${DB_TLS_DIR:-/certs}
TLS_DST=/run/survey-db-tls   # /run 아래: deploy 는 읽기 전용 루트에 /tmp·/run(tmpfs)과 데이터 볼륨만 쓸 수 있게 준다
[ -r "$TLS_SRC/tls.crt" ] && [ -r "$TLS_SRC/tls.key" ] || { echo "survey-db: $TLS_SRC 에 tls.crt·tls.key 가 없다(서버 TLS 필수)" >&2; exit 1; }
copy_tls() { install -d -m 700 "$TLS_DST"; install -m 600 "$TLS_SRC/tls.key" "$TLS_DST/tls.key"; install -m 644 "$TLS_SRC/tls.crt" "$TLS_DST/tls.crt"; }      # 이미 postgres 로 돈다: chown 이 필요 없다(소유자는 이 프로세스)
copy_tls
# 배치 비밀(0640)을 완화하지 않고 postgres 전용 tmpfs 사본(0600)을 준비한다. 배치 쪽은 선언(inputs.json)의 uid 999 에 읽기 권한을 준다.
secret_source=${DB_ACCOUNT_SECRETS_DIR:-/run/secrets/db}
secret_target=/run/survey-db-secrets
install -d -m 0700 "$secret_target"
for account in $(cut -f1 /etc/openit/accounts.tsv); do
  file="$secret_source/$account"
  [ -r "$file" ] && [ -s "$file" ] || { echo "survey-service-db: 계정 $account 의 암호 파일을 읽을 수 없거나 비어 있다" >&2; exit 1; }
  first_line=$(head -n1 "$file") || { echo "survey-service-db: 계정 $account 의 암호 파일 읽기 실패" >&2; exit 1; }
  [ -n "$first_line" ] || { echo "survey-service-db: 계정 $account 의 암호 첫 줄이 비어 있다" >&2; exit 1; }
  unset first_line
  install -m 0600 "$file" "$secret_target/$account"
done
export DB_ACCOUNT_SECRETS_DIR="$secret_target"
(
  last=$(cksum "$TLS_SRC/tls.crt" "$TLS_SRC/tls.key")
  while sleep 60; do
    now=$(cksum "$TLS_SRC/tls.crt" "$TLS_SRC/tls.key" 2>/dev/null) || continue
    if [ "$now" != "$last" ]; then copy_tls && psql -qAt -c 'SELECT pg_reload_conf()' >/dev/null 2>&1 && last=$now; fi
  done
) &
# 서버 설정(명령줄 -c): TLS 필수, scram, 연결 상한. 연결 상한 50은 이 영역 앱의 연결(저장소 4개와 확인·잡)에 여유를 둔 시작값이고 측정하지 않았다(ADR-0063)
exec docker-entrypoint.sh postgres \
  -c listen_addresses='*' -c ssl=on -c ssl_cert_file="$TLS_DST/tls.crt" -c ssl_key_file="$TLS_DST/tls.key" -c ssl_min_protocol_version=TLSv1.2 \
  -c password_encryption=scram-sha-256 -c max_connections=50 "$@"
