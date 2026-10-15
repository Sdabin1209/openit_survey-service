# survey-service 이미지. 설정은 환경 변수로만 받고 비밀 값은 넣지 않는다. 로그는 표준 출력. 비root로 실행.
# 빌드 전에 scripts/build_wheels.sh 로 build/wheels 를 만든다(이웃 라이브러리는 Git 태그 시점의 소스로 만든 wheel).
FROM python:3.12-slim AS build
COPY build/wheels /wheels
COPY requirements.lock /requirements.lock
RUN pip install --no-cache-dir --prefix=/install -r /requirements.lock \
 && pip install --no-cache-dir --prefix=/install --no-deps /wheels/*.whl

FROM python:3.12-slim
ARG COMMIT=unknown
ARG BOUNDARY_TAG=unknown
ARG SCHEMA_CONTRACT_VERSION=unknown
ARG TURN_ENGINE_TAG=unknown
ARG MODEL_RUNTIME_TAG=unknown
LABEL io.openit.service=survey-service \
      io.openit.revision=$COMMIT \
      io.openit.contracts.boundary=$BOUNDARY_TAG \
      io.openit.contracts.survey-schema=$SCHEMA_CONTRACT_VERSION \
      io.openit.turn-engine=$TURN_ENGINE_TAG \
      io.openit.model-runtime=$MODEL_RUNTIME_TAG
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --no-create-home app && mkdir /data && chown app /data
USER app
ENV PYTHONUNBUFFERED=1 \
    SURVEY_PORT=8080 \
    SURVEY_SESSION_DB=/data/sessions.db \
    SURVEY_OUTBOX_DB=/data/outbox.db \
    SURVEY_WORK_DIR=/data/releases
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD ["python", "-c", "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SURVEY_PORT','8080')+'/healthz')"]
CMD ["python", "-m", "survey_service"]
