# survey-db: 이 영역의 데이터베이스 컴포넌트 이미지(계약 D126). 공식 postgres 이미지 위에 초기화(계정·권한·pg_hba·TLS·연결 상한)를 넣는다.
# 입력은 AreaDatabaseEnv(POSTGRES_PASSWORD_FILE, DB_ACCOUNT_SECRETS_DIR, DB_TLS_DIR). 선언은 라벨 io.openit.database(DatabaseDeclaration)다.
# 기반 이미지는 태그가 아니라 digest로 고정한다(Docker 공식 권고: 태그는 옮겨 가지만 digest는 내용이 같다. 공통 규칙 '빌드 때 고정'). 이 값은 2026-11-05에 이 PC에 있는 같은 태그의 RepoDigest(이미지 생성 2026-10-28)를 읽은 것이다. 올리는 법: `docker pull postgres:16` 뒤 `docker image inspect postgres:16 --format '{{index .RepoDigests 0}}'`의 값으로 이 줄 하나를 바꾸고 이미지 빌드 시험·스모크를 다시 돌린다.
FROM postgres:16@sha256:1beaa3084eee2676f20a9cd9928f0bf2c5bf674f8e49f81017912183d377c61c
ARG COMMIT
ARG BOUNDARY_TAG
ARG DATABASE_DECLARATION
RUN echo "$COMMIT" | grep -Eq '^[0-9a-f]{7,40}$' && echo "$BOUNDARY_TAG" | grep -Eq '^v[0-9]+[.][0-9]+[.][0-9]+$' && test -n "$DATABASE_DECLARATION" \
 || { echo "빌드 인자(COMMIT 해시, BOUNDARY_TAG v접두 버전, DATABASE_DECLARATION)가 라벨 규칙에 맞지 않는다" >&2; exit 1; }
LABEL io.openit.service=survey-db \
      io.openit.variant=db \
      io.openit.revision=$COMMIT \
      io.openit.contracts.boundary=$BOUNDARY_TAG \
      io.openit.contracts.survey-schema=n/a \
      io.openit.database=$DATABASE_DECLARATION
COPY db/accounts.tsv /etc/openit/accounts.tsv
COPY db/initdb.d/ /docker-entrypoint-initdb.d/
COPY db/entrypoint.sh /usr/local/bin/survey-db-entrypoint.sh
COPY db/healthcheck.sh /usr/local/bin/survey-db-healthcheck.sh
RUN chmod +x /docker-entrypoint-initdb.d/*.sh /usr/local/bin/survey-db-entrypoint.sh /usr/local/bin/survey-db-healthcheck.sh
# HEALTHCHECK 값(10초 간격, 5초 제한, 시작 유예 90초, 3회): 측정하지 않은 시작 값. 시작 유예는 initdb(계정·권한 생성)가 끝날 시간, ADR-0063
# 왜 USER postgres: 공통 규칙 'root가 아닌 사용자'. 공식 이미지의 postgres 사용자(uid 999)다. 주입 선언 db/inputs.json 의 process 와 같아야 하고 build-image.sh 가 대조한다. 공식 entrypoint 는 root 가 아니면 gosu·chown 없이 그대로 initdb 한다.
USER postgres
HEALTHCHECK --interval=10s --timeout=5s --start-period=90s --retries=3 CMD ["/usr/local/bin/survey-db-healthcheck.sh"]
ENTRYPOINT ["/usr/local/bin/survey-db-entrypoint.sh"]
# 공식 이미지의 CMD(postgres)를 비운다: 진입점이 docker-entrypoint.sh postgres 에 서버 설정을 붙여 부른다
CMD []
