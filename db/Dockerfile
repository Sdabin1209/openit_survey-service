# survey-db: 이 영역의 데이터베이스 컴포넌트 이미지(계약 D126). 공식 postgres 이미지 위에 초기화(계정·권한·pg_hba·TLS·연결 상한)를 넣는다.
# 입력은 AreaDatabaseEnv(POSTGRES_PASSWORD_FILE, DB_ACCOUNT_SECRETS_DIR, DB_TLS_DIR). 선언은 라벨 io.openit.database(DatabaseDeclaration)다.
FROM postgres:16
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
HEALTHCHECK --interval=10s --timeout=5s --start-period=90s --retries=3 CMD ["/usr/local/bin/survey-db-healthcheck.sh"]
ENTRYPOINT ["/usr/local/bin/survey-db-entrypoint.sh"]
# 공식 이미지의 CMD(postgres)를 비운다: 진입점이 docker-entrypoint.sh postgres 에 서버 설정을 붙여 부른다
CMD []
