# survey-service 이미지. 설정은 환경 변수로만 받고 비밀 값은 넣지 않는다. 로그는 표준 출력. 비root로 실행.
# 이웃 라이브러리(boundary-contracts, model-runtime)는 이름 있는 빌드 컨텍스트(--build-context boundary_contracts=..., model_runtime=...)로 받는다:
# 영역 빌드 진입점 scripts/build-image.sh가 요청의 dependency_sources(해당 버전 소스 트리)를 준다(계약 2.5.0). 이 Dockerfile은 네트워크로 이웃을 받지 않는다(서드파티는 잠금 파일에서 PyPI).
# 기반 이미지는 태그가 아니라 digest로 고정한다(Docker 공식 권고: 태그는 옮겨 가지만 digest는 내용이 같다. 공통 규칙 '빌드 때 고정'). 이 값은 2026-11-05에 이 PC에 있는 같은 태그의 RepoDigest(이미지 생성 2026-10-28)를 읽은 것이다. 올리는 법: `docker pull python:3.12-slim` 뒤 `docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'`의 값으로 이 줄 하나를 바꾸고 이미지 빌드 시험·스모크를 다시 돌린다.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
FROM ${PYTHON_IMAGE} AS build
COPY requirements.lock /requirements.lock
RUN pip install --no-cache-dir --prefix=/install -r /requirements.lock
COPY --from=boundary_contracts . /src/boundary-contracts
COPY --from=model_runtime . /src/model-runtime
COPY --from=openit_link . /src/link
COPY schema /src/schema
COPY pyproject.toml /src/app/pyproject.toml
COPY survey_service /src/app/survey_service
RUN pip install --no-cache-dir --prefix=/install --no-deps /src/boundary-contracts /src/model-runtime /src/link /src/schema /src/app

FROM ${PYTHON_IMAGE}
ARG COMMIT
ARG BOUNDARY_TAG
ARG SURVEY_SCHEMA_MAJOR
ARG MODEL_RUNTIME_TAG
ARG LANGGRAPH_VERSION
# 왜: 라벨 값이 deploy의 ImageLabels(게시 전 검사)를 통과하는 형식이 아니면 이미지를 만들지 않는다(기본값 unknown이 라벨에 남지 않게).
RUN echo "$COMMIT" | grep -Eq '^[0-9a-f]{7,40}$' && echo "$SURVEY_SCHEMA_MAJOR" | grep -Eq '^([0-9]+|n/a)$' \
 && for t in "$BOUNDARY_TAG" "$MODEL_RUNTIME_TAG"; do echo "$t" | grep -Eq '^v[0-9]+[.][0-9]+[.][0-9]+$' || exit 1; done \
 && test -n "$LANGGRAPH_VERSION" || { echo "빌드 인자(COMMIT 해시, SURVEY_SCHEMA_MAJOR 정수, *_TAG v접두 버전, LANGGRAPH_VERSION)가 라벨 규칙에 맞지 않는다" >&2; exit 1; }
LABEL io.openit.service=survey-service \
      io.openit.revision=$COMMIT \
      io.openit.contracts.boundary=$BOUNDARY_TAG \
      io.openit.contracts.survey-schema=$SURVEY_SCHEMA_MAJOR \
      io.openit.model-runtime=$MODEL_RUNTIME_TAG \
      io.openit.langgraph=$LANGGRAPH_VERSION
# 백업 잡(python -m survey_service backup)이 쓰는 restic: 백업 저장소 프로토콜(restic REST)의 표준 클라이언트(ADR-0060). 배포판 패키지이고 버전은 이미지 빌드 때의 배포판 값이다
RUN apt-get update && apt-get install -y --no-install-recommends restic ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --no-create-home app && mkdir /data && chown app /data
USER app
# LangChain 계열의 외부 추적(LangSmith)을 끈다: 문진 발화가 밖으로 나가면 안 된다. API 키는 이미지에 없다.
ENV LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false \
    LANGCHAIN_TRACING=false \
    PYTHONUNBUFFERED=1 \
    SURVEY_PORT=8080 \
    SURVEY_SESSION_DB=/data/sessions.db \
    SURVEY_OUTBOX_DB=/data/outbox.db \
    SURVEY_WORK_DIR=/data/releases
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD ["python", "-c", "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SURVEY_PORT','8080')+'/healthz')"]
CMD ["python", "-m", "survey_service"]
