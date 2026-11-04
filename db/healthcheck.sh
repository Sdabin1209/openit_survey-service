#!/bin/bash
# survey-db HEALTHCHECK: 선언된 첫 계정으로 TLS 접속해 `SELECT 1`이 되고 그 연결이 실제로 TLS인지(pg_stat_ssl) 확인한다. 하나라도 어긋나면 종료 코드 1(unhealthy).
# 왜 선언 계정으로 접속하나: pg_isready는 서버가 연결을 받는지만 보고, 계정·암호·pg_hba(hostssl)·TLS가 맞는지는 모른다. 영역의 DB 보장(D126)을 이미지 안에서 확인한다.
# 왜 sslmode=require: 컨테이너 안에는 루트 인증서가 없고 같은 컨테이너의 루프백이다. 인증서 이름 검증(verify-full)은 클라이언트(서비스)가 한다. 여기서는 암호화된 연결이 되는지만 본다.
set -euo pipefail
SECRETS=${DB_ACCOUNT_SECRETS_DIR:-/run/secrets/db}
IFS=$'\t' read -r name db _priv < /etc/openit/accounts.tsv
passfile=$(mktemp); trap 'rm -f "$passfile"' EXIT; chmod 600 "$passfile"
printf '127.0.0.1:5432:%s:%s:%s\n' "$db" "$name" "$(head -n1 "$SECRETS/$name" | sed 's/[:\\]/\\&/g')" > "$passfile"
out=$(PGPASSFILE="$passfile" PGCONNECT_TIMEOUT=3 psql "host=127.0.0.1 port=5432 dbname=$db user=$name sslmode=require" -qAt -c "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
[ "$out" = t ]
