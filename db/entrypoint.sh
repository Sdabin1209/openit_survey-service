#!/bin/bash
# survey-db 진입점(root로 시작): TLS 인증서를 postgres가 읽을 수 있게 복사하고 공식 entrypoint로 넘긴다. 서버 TLS 필수(AreaDatabaseEnv.DB_TLS_DIR의 tls.crt·tls.key).
# 왜 복사하나: PostgreSQL은 개인 키가 그룹·모두에게 읽히면(0640 초과, 소유자가 root가 아니면 0600 초과) 서버 시작을 거부한다. 읽기 전용 마운트는 권한을 바꿀 수 없다.
# 인증서는 24시간마다 새로 발급된다(step-ca): 60초마다 원본이 바뀌었는지 보고 바뀌면 복사 뒤 reload한다(재시작 없음).
set -euo pipefail
TLS_SRC=${DB_TLS_DIR:-/certs}
TLS_DST=/run/survey-db-tls   # /run 아래: deploy 는 읽기 전용 루트에 /tmp·/run(tmpfs)과 데이터 볼륨만 쓸 수 있게 준다
[ -r "$TLS_SRC/tls.crt" ] && [ -r "$TLS_SRC/tls.key" ] || { echo "survey-db: $TLS_SRC 에 tls.crt·tls.key 가 없다(서버 TLS 필수)" >&2; exit 1; }
copy_tls() { install -d -o postgres -g postgres -m 700 "$TLS_DST"; install -o postgres -g postgres -m 600 "$TLS_SRC/tls.key" "$TLS_DST/tls.key"; install -o postgres -g postgres -m 644 "$TLS_SRC/tls.crt" "$TLS_DST/tls.crt"; }
copy_tls
(
  last=$(cksum "$TLS_SRC/tls.crt" "$TLS_SRC/tls.key")
  while sleep 60; do
    now=$(cksum "$TLS_SRC/tls.crt" "$TLS_SRC/tls.key" 2>/dev/null) || continue
    if [ "$now" != "$last" ]; then copy_tls && gosu postgres psql -qAt -c 'SELECT pg_reload_conf()' >/dev/null 2>&1 && last=$now; fi
  done
) &
# 서버 설정(명령줄 -c): TLS 필수, scram, 연결 상한. 연결 상한 50은 이 영역 앱의 연결(저장소 4개와 확인·잡)에 여유를 둔 시작값이고 측정하지 않았다(ADR-0063)
exec docker-entrypoint.sh postgres \
  -c listen_addresses='*' -c ssl=on -c ssl_cert_file="$TLS_DST/tls.crt" -c ssl_key_file="$TLS_DST/tls.key" -c ssl_min_protocol_version=TLSv1.2 \
  -c password_encryption=scram-sha-256 -c max_connections=50 "$@"
