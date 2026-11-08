#!/bin/bash
# survey-db 초기화(계약 D126·AreaDatabaseEnv): 공식 postgres 이미지의 initdb 표준 훅(/docker-entrypoint-initdb.d, 데이터 폴더가 비어 있을 때 한 번만 실행)에서
# 선언된 계정·데이터베이스·권한·접속 규칙을 만든다. 입력은 /etc/openit/accounts.tsv(이름 TAB 데이터베이스 TAB owner|dml, 빌드가 DatabaseDeclaration에서 만든다)와
# DB_ACCOUNT_SECRETS_DIR(계정마다 암호 파일 하나). 암호는 표준 입력의 psql 메타명령(\set pw `head`) + format(%L)로만(명령행 인자에 두지 않는다) 넣는다(SQL 문자열에 이어 붙이지 않는다).
set -euo pipefail
ACCOUNTS=/etc/openit/accounts.tsv
SECRETS=${DB_ACCOUNT_SECRETS_DIR:-/run/secrets/db}
NAME_RE='^[a-z][a-z0-9_]{0,30}$'
PSQL=(psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --no-psqlrc)

# 1) 계정: LOGIN만, 슈퍼유저·DB/롤 생성·복제 권한 없음. 암호는 scram-sha-256로 저장(password_encryption)
while IFS=$'\t' read -r name db priv; do
  [[ "$name" =~ $NAME_RE && "$db" =~ $NAME_RE && ( "$priv" == owner || "$priv" == dml ) ]] || { echo "survey-db: 잘못된 선언 줄" >&2; exit 1; }
  [ -s "$SECRETS/$name" ] || { echo "survey-db: 계정 $name 의 암호 파일이 없거나 비었다" >&2; exit 1; }
  # 왜 -v pw= 가 아닌가: 명령행 인자는 /proc/<pid>/cmdline(ps)에 보인다. 암호 파일 경로만 표준 입력의 메타명령(\set pw `head ...`)에 두면 psql 이 읽어 넣고, 값은 인자에도 환경에도 나타나지 않는다(경로는 NAME_RE 로 검증됨)
  "${PSQL[@]}" --dbname postgres -v who="$name" <<SQL
\\set pw \`head -n1 "$SECRETS/$name"\`
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT PASSWORD %L', :'who', :'pw') \\gexec
SQL
done < "$ACCOUNTS"

# 2) 데이터베이스: owner 계정이 자기 데이터베이스를 소유한다. PUBLIC의 CONNECT를 회수하고 선언된 계정에만 준다
while IFS=$'\t' read -r name db priv; do
  [ "$priv" = owner ] || continue
  "${PSQL[@]}" --dbname postgres -v who="$name" -v db="$db" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'who') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SQL
done < "$ACCOUNTS"

# 2-1) 기본 데이터베이스(postgres)의 PUBLIC CONNECT도 회수한다: 선언된 계정이 선언 밖 데이터베이스에 붙지 못하게 한다(슈퍼유저 postgres는 그대로)
"${PSQL[@]}" --dbname postgres <<'SQL'
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
SQL

# 3) 계정마다 권한. dml: CONNECT·스키마 USAGE·표 SELECT/INSERT/UPDATE/DELETE·시퀀스 USAGE/SELECT, 그리고 owner가 앞으로 만들 표·시퀀스에도(기본 권한)
while IFS=$'\t' read -r name db priv; do
  owner=$(awk -F'\t' -v d="$db" '$2==d && $3=="owner"{print $1; exit}' "$ACCOUNTS")
  "${PSQL[@]}" --dbname postgres -v who="$name" -v db="$db" <<'SQL'
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'who') \gexec
SQL
  [ "$priv" = dml ] || continue
  "${PSQL[@]}" --dbname "$db" -v who="$name" -v owner="$owner" <<'SQL'
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'who') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', :'owner', :'who') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I', :'owner', :'who') \gexec
SQL
done < "$ACCOUNTS"

# 4) 접속 규칙(pg_hba.conf): 루프백 trust 없음. 소켓은 컨테이너 안의 postgres OS 사용자(peer)뿐이고, 네트워크는 TLS(hostssl) + scram-sha-256뿐이다. 평문 host 줄이 없어 평문 접속은 거절된다
cat > "$PGDATA/pg_hba.conf" <<'HBA'
local   all   postgres                 peer
hostssl all   all        0.0.0.0/0     scram-sha-256
hostssl all   all        ::/0          scram-sha-256
HBA
