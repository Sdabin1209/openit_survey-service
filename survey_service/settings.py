"""설정은 환경 변수로만 받는다. 읽는 곳은 서비스 진입점뿐이다(CONVENTIONS 7절)."""
from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional


class ConfigError(Exception):
    pass


def _int(env: Mapping, key: str, default: int, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{key}는 정수여야 한다") from None
    if v < minimum:
        raise ConfigError(f"{key}는 {minimum} 이상이어야 한다")
    return v


def _retention(env: Mapping, key: str, default: int) -> int:
    """보존 기간(일). 기본값이 있고 무기한(null·0·빈 값·none)은 거부한다(D88 U4, ADR-0015)."""
    raw = env.get(key)
    if raw is None:
        return default
    try:
        v = int(str(raw).strip())
    except ValueError:
        raise ConfigError(f"{key}는 1 이상의 정수(일)여야 한다. 무기한은 허용하지 않는다") from None
    if v < 1:
        raise ConfigError(f"{key}는 1 이상이어야 한다. 무기한은 허용하지 않는다")
    return v


def _float(env: Mapping, key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        raise ConfigError(f"{key}는 숫자여야 한다") from None
    if v <= 0:
        raise ConfigError(f"{key}는 0보다 커야 한다")
    return v


# 계약 SurveyServiceEnv(boundary-contracts)에 아직 없는 이 영역의 설정 이름. CR-021이 반영되면 지운다.
_NOT_YET_IN_CONTRACT = ("SURVEY_TURN_ENGINE_VERSION",)


def validate_env(env: Mapping[str, str]) -> None:
    """환경 변수를 계약 SurveyServiceEnv 스키마로 검증한다(U44, ADR-0038): 필수 이름, 값 형식, 그리고 알려지지 않은 SURVEY_* 이름(오타)을 시작 때 거부한다.
    오류 문구에는 이름만 싣고 값은 싣지 않는다(값이 비밀일 수 있다)."""
    from boundary_contracts import validator
    checked = {k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str) and k not in _NOT_YET_IN_CONTRACT}
    unknown, bad, missing = [], [], []
    for e in validator("SurveyServiceEnv").iter_errors(checked):
        if e.validator == "required":
            missing.append(re.match(r"'([^']+)'", e.message).group(1))
        elif not e.path and isinstance(e.instance, str):           # propertyNames: 계약 목록에 없는 SURVEY_* 이름
            unknown.append(e.instance)
        elif e.path:
            bad.append(str(e.path[0]))
        else:
            bad.append("(환경 변수 전체)")
    parts = []
    if unknown:
        parts.append("모르는 SURVEY_* 설정 이름(오타인지 확인): " + ", ".join(sorted(set(unknown))))
    if missing:
        parts.append("필수 환경 변수가 없다: " + ", ".join(sorted(set(missing))))
    if bad:
        parts.append("계약 SurveyServiceEnv 형식에 맞지 않는 값: " + ", ".join(sorted(set(bad))))
    if parts:
        raise ConfigError("; ".join(parts))


@dataclass(frozen=True)
class Settings:
    port: int = 8080
    session_db: str = "/data/sessions.db"
    outbox_db: str = "/data/outbox.db"
    work_dir: str = "/data/releases"
    release_reader: str = ""        # I4 구현 팩토리 "모듈:함수"
    trace_ingest: str = ""          # I6 구현 팩토리
    model_backend: str = ""         # 예: fake
    staff_console: str = ""         # 선택 팩토리. 없으면 알림 전달은 실패로 기록된다
    result_dispatcher: str = ""     # 선택 팩토리. 없으면 결과 전달은 실패로 기록된다
    risk_source: str = ""           # 선택 팩토리(RiskSource). 없으면 규칙 감지(기본 임시 목록, risk_default_rules)만 쓴다. 둘 다 꺼지면 입력원이 없고 /readyz에 active=false로 드러난다(CR-012)
    result_reader_token_env: str = "SURVEY_RESULT_READER_TOKEN"   # 결과 조회(역할 result-reader) 토큰이 든 변수 이름. 값이 없으면 /v1/results는 꺼진다(404)
    risk_source_token_env: str = "SURVEY_RISK_SOURCE_TOKEN"       # 외부 위험 신호(역할 risk-source) 토큰. 값이 없으면 risk-signals는 꺼진다(404)
    require_subject_ref: bool = False                              # 켜면 세션 시작에 subject_ref가 필요하다(기본 꺼짐, 데모용)
    session_retention_days: int = 90                               # 끝난 세션 보존(일). D88 U4 잠정값, 병원 규정이 다르면 설정만 바꾼다. 끌 수 없다(null·0 거부) (ADR-0015)
    access_log_retention_days: int = 365                           # staff_access_log 보존(일)
    alert_retention_days: int = 365                                # 해결된 알림 보존(일, 해결 시각 기준). 열린 알림은 지우지 않는다
    dead_outbox_retention_days: int = 30                           # DEAD로 끝난 Trace 행 보존(일)
    retention_interval_seconds: int = 3600                         # 서비스 안에서 보존 정리를 도는 주기(초). 0이면 서비스는 돌리지 않는다(deploy의 purge 잡만)
    risk_default_rules: bool = True  # 규칙 파일·입력원이 없으면 임시 기본 목록(임상 검토 전)을 쓴다. SURVEY_RISK_DEFAULT_RULES=off로 끈다 (ADR-0013)
    risk_rules_file: str = ""       # 위험 감지 규칙 파일(JSON, survey_service/risk_rules.py). 없으면 비활성. risk_source와 함께 쓸 수 없다
    turn_deadline_seconds: float = 6.0            # 서버 턴 총 마감(초). 키오스크 대기 10초보다 짧게. 넘으면 NOT_COMMITTED(FAULT_POLICY 3)  [근거: ADR-0008, 6초는 DB 장애 pause 5.0초 측정과 gateway 상한에서 정함]
    db_connect_timeout_seconds: float = 2.0       # PostgreSQL 주 연결의 접속 상한(초). 이전 고정값은 10초  [근거: ADR-0011]
    db_statement_timeout_seconds: float = 2.0     # 문장 하나의 상한(초). 서버 statement_timeout + 클라이언트 강제 끊기  [근거: ADR-0011]
    db_reconnect_backoff_seconds: float = 1.0     # 접속 실패 뒤 이 시간 동안은 재접속을 시도하지 않고 바로 실패(빠른 실패)  [근거: ADR-0011]
    extract_input_max_chars: int = 2400           # 추출 입력(JSON) 길이 상한. model-runtime의 상한 2500자보다 약간 낮게(여유 100). 넘으면 발화를 줄여 보낸다 (ADR-0012)
    socket_timeout_seconds: float = 15.0          # HTTP 연결 무활동 제한(초, waitress channel_timeout, 올림해 정수 초). 정상 요청은 수 ms~수 초, 길게 매달린 연결은 끊는다 (ADR-0018, 측정 안 함)
    max_connections: int = 256                    # 동시 접속 상한(SSE 포함). 넘으면 새 연결을 받지 않고 커널 대기열에 둔다(waitress connection_limit, ADR-0029). 처리 단계 상한(api_max_inflight 32)과 별개  [근거: ADR-0018, 값은 측정 안 함]
    max_sse_connections: int = 50                 # 직원 알림 스트림 동시 연결 상한  [근거: ADR-0018, 값은 측정 안 함]
    readyz_db_timeout_seconds: float = 2.0        # /readyz가 PostgreSQL을 확인하는 데 쓰는 상한(초). 넘으면 503 db unavailable. SQLite에는 해당 없음
    release_poll_seconds: float = 5.0
    startup_wait_seconds: float = 5.0             # 시작할 때 첫 릴리스 적재를 기다리는 최대 시간(넘으면 기다리지 않고 서비스를 연다)
    release_retry_base_seconds: float = 5.0       # 일시적 적재 실패의 재시도 백오프 시작
    release_retry_max_seconds: float = 60.0       # 백오프 최대
    release_retry_give_up_seconds: float = 0.0    # 0이면 상한 없음(계속 재시도)
    # HTTP 클라이언트(release_reader/trace_ingest가 'http'일 때): 토큰은 값이 아니라 '환경 변수 이름'으로 받는다
    release_reader_url: str = ""
    release_reader_token_env: str = "SURVEY_RELEASE_READER_TOKEN"
    trace_ingest_url: str = ""
    trace_ingest_token_env: str = "SURVEY_TRACE_INGEST_TOKEN"
    http_timeout_seconds: float = 10.0
    # 공개 API(키오스크 프론트, PUBLIC_API.md). 토큰은 값이 아니라 '환경 변수 이름'으로 받는다
    api_token_env: str = "SURVEY_API_TOKEN"
    alert_db: str = ""                                 # 직원 알림 영속 저장(SQLite). 비우면 세션 DB와 같은 디렉터리의 alerts.db
    vapid_private_key_env: str = "SURVEY_VAPID_PRIVATE_KEY"   # 웹 푸시 VAPID 비밀키가 든 환경 변수의 이름(없으면 푸시 없이 SSE만)
    vapid_subject: str = ""                            # mailto:... 또는 https://... (비밀 아님)
    push_allow_private_hosts: bool = False        # 개발·시험 전용: 웹 푸시 endpoint가 사설·내부 주소여도 허용(운영은 false: SSRF 방어)
    push_timeout_seconds: float = 3.0
    sse_heartbeat_seconds: float = 15.0          # ': ping' 주석을 보내는 간격
    retry_after_seconds: int = 1                 # 429·503 응답의 Retry-After(초)
    state_poll_min_interval_seconds: float = 0.5  # 세션 상태 폴링 빈도 상한(세션마다, 0이면 끔)
    max_push_subscriptions: int = 100
    kiosk_device_tokens_env: str = "SURVEY_KIOSK_DEVICE_TOKENS"   # 기기별 키오스크 토큰 표(`device_id=토큰` 줄). NAME_FILE이 있으면 파일 내용 (D88 U8)
    staff_api_token_env: str = "SURVEY_STAFF_API_TOKEN"    # 직원 개입용 별도 토큰(역할 staff)
    api_max_inflight: int = 32  # [근거: ADR-0004, 부하 측정 boundary.md §6-1 (기본 32)]
    api_max_utterance_chars: int = 1000
    session_idle_seconds: int = 300          # 0이면 유휴 만료를 하지 않는다
    # 모델 런타임을 HTTP 경계 뒤에 둘 때(추론 컨테이너): 비우면 프로세스 안에서 적재한다
    inference_url: str = ""
    inference_token_env: str = "SURVEY_INFERENCE_TOKEN"
    inference_timeout_seconds: float = 4.0      # 추론 서버 제어 호출(릴리스 적재 확인, /readyz)의 타임아웃. 환자 턴의 extract·respond에는 쓰지 않는다 (ADR-0026)
    inference_extract_timeout_seconds: float = 3.0   # 환자 턴 extract 한 번의 상한. 계약 CONVENTIONS 2.2.0 표의 배분값, 측정하지 않았다 (ADR-0026)
    inference_respond_timeout_seconds: float = 2.0   # 환자 턴 respond 한 번의 상한. 같은 표, 측정하지 않았다 (ADR-0026)
    inference_load_timeout_seconds: float = 600.0   # 릴리스 적재(모델 로딩)를 기다리는 시간
    inference_retries: int = 1
    staff_call_retry_min_interval_seconds: int = 30   # 직원 호출 재알림 최소 간격(CR-H001). 계약 기본 권고. 측정하지 않았다 (ADR-0021)
    staff_call_retry_max_per_session: int = 10        # 세션당 재알림 상한. 계약 기본 권고. 측정하지 않았다 (ADR-0021)
    staff_call_report_max_per_session: int = 20       # 세션당 직접 알림 보고 상한. 측정하지 않았다 (ADR-0021)
    turn_engine_version: str = "0.4.3"          # 릴리스 매니페스트의 turn_engine_version과 MAJOR.MINOR를 비교하는 기준(D45, CR-005). 코드는 turn-engine에 의존하지 않으므로(D109) 라이브러리 설치본이 아니라 배포가 주는 값이다. 기본 0.4.3은 헤드 기준값(D109)이고 측정한 값이 아니다 (ADR-0028)
    max_consecutive_reask: int = 3              # REASK·CONFIRM 연속 상한(G12, D88 U10 잠정값 3). 0이면 끔(명시해야 꺼진다). 문항 세트의 ask_limit 등이 있으면 그 최솟값을 쓴다 (ADR-0014)
    keep_releases: int = 2
    export_interval_seconds: float = 1.0
    export_backoff_max_seconds: float = 60.0
    outbox_max_pending: int = 20000               # 보내지 못한 Trace의 최대 건수(넘으면 새 Trace는 버리고 건수를 센다). 0이면 상한 없음  [근거: ADR-0009, 크기는 추정]
    outbox_keep_sent_hours: int = 24
    outbox_stage_grace_seconds: float = 60.0      # 턴 진행 중 예약 행이 이만큼 오래되면 턴이 사라진 것으로 보고 전송 대상으로 승격. 서버 턴 마감 6초의 10배, 측정하지 않았다 (ADR-0023)
    log_level: str = "INFO"

    def alert_db_path(self) -> str:
        return self.alert_db or os.path.join(os.path.dirname(self.session_db) or ".", "alerts.db")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        validate_env(env)
        s = cls(
            port=_int(env, "SURVEY_PORT", 8080, 1), session_db=env.get("SURVEY_SESSION_DB", cls.session_db),
            outbox_db=env.get("SURVEY_OUTBOX_DB", cls.outbox_db), work_dir=env.get("SURVEY_WORK_DIR", cls.work_dir),
            release_reader=env.get("SURVEY_RELEASE_READER", ""), trace_ingest=env.get("SURVEY_TRACE_INGEST", ""),
            model_backend=env.get("SURVEY_MODEL_BACKEND", ""), staff_console=env.get("SURVEY_STAFF_CONSOLE", ""),
            result_dispatcher=env.get("SURVEY_RESULT_DISPATCHER", ""), risk_source=env.get("SURVEY_RISK_SOURCE", ""),
            risk_default_rules=env.get("SURVEY_RISK_DEFAULT_RULES", "on").strip().lower() not in ("0", "false", "no", "off"),
            risk_rules_file=env.get("SURVEY_RISK_RULES_FILE", ""),
            result_reader_token_env=env.get("SURVEY_RESULT_READER_TOKEN_ENV", "SURVEY_RESULT_READER_TOKEN"),
            risk_source_token_env=env.get("SURVEY_RISK_SOURCE_TOKEN_ENV", "SURVEY_RISK_SOURCE_TOKEN"),
            require_subject_ref=env.get("SURVEY_REQUIRE_SUBJECT_REF", "").strip().lower() in ("1", "true", "yes", "on"),
            session_retention_days=_retention(env, "SURVEY_SESSION_RETENTION_DAYS", 90),
            access_log_retention_days=_retention(env, "SURVEY_ACCESS_LOG_RETENTION_DAYS", 365),
            alert_retention_days=_retention(env, "SURVEY_ALERT_RETENTION_DAYS", 365),
            dead_outbox_retention_days=_retention(env, "SURVEY_DEAD_OUTBOX_RETENTION_DAYS", 30),
            retention_interval_seconds=_int(env, "SURVEY_RETENTION_INTERVAL_SECONDS", 3600, 0),
            release_reader_url=env.get("SURVEY_RELEASE_READER_URL", ""),
            release_reader_token_env=env.get("SURVEY_RELEASE_READER_TOKEN_ENV", "SURVEY_RELEASE_READER_TOKEN"),
            trace_ingest_url=env.get("SURVEY_TRACE_INGEST_URL", ""),
            trace_ingest_token_env=env.get("SURVEY_TRACE_INGEST_TOKEN_ENV", "SURVEY_TRACE_INGEST_TOKEN"),
            api_token_env=env.get("SURVEY_API_TOKEN_ENV", "SURVEY_API_TOKEN"),
            alert_db=env.get("SURVEY_ALERT_DB", ""),
            vapid_private_key_env=env.get("SURVEY_VAPID_PRIVATE_KEY_ENV", "SURVEY_VAPID_PRIVATE_KEY"),
            vapid_subject=env.get("SURVEY_VAPID_SUBJECT", ""),
            push_timeout_seconds=_float(env, "SURVEY_PUSH_TIMEOUT_SECONDS", 3.0),
            push_allow_private_hosts=env.get("SURVEY_PUSH_ALLOW_PRIVATE_HOSTS", "").strip().lower() in ("1", "true", "yes", "on"),
            retry_after_seconds=_int(env, "SURVEY_RETRY_AFTER_SECONDS", 1, 1),
            state_poll_min_interval_seconds=float(env.get("SURVEY_STATE_POLL_MIN_INTERVAL_SECONDS", "0.5")),
            sse_heartbeat_seconds=_float(env, "SURVEY_SSE_HEARTBEAT_SECONDS", 15.0),
            max_push_subscriptions=_int(env, "SURVEY_MAX_PUSH_SUBSCRIPTIONS", 100, 1),
            staff_api_token_env=env.get("SURVEY_STAFF_API_TOKEN_ENV", "SURVEY_STAFF_API_TOKEN"),
            api_max_inflight=_int(env, "SURVEY_API_MAX_INFLIGHT", 32, 1),
            api_max_utterance_chars=_int(env, "SURVEY_API_MAX_UTTERANCE_CHARS", 1000, 1),
            session_idle_seconds=_int(env, "SURVEY_SESSION_IDLE_SECONDS", 300, 0),
            inference_url=env.get("SURVEY_INFERENCE_URL", ""),
            inference_token_env=env.get("SURVEY_INFERENCE_TOKEN_ENV", "SURVEY_INFERENCE_TOKEN"),
            inference_timeout_seconds=_float(env, "SURVEY_INFERENCE_TIMEOUT_SECONDS", 4.0),
            inference_extract_timeout_seconds=_float(env, "SURVEY_INFERENCE_EXTRACT_TIMEOUT_SECONDS", 3.0),
            inference_respond_timeout_seconds=_float(env, "SURVEY_INFERENCE_RESPOND_TIMEOUT_SECONDS", 2.0),
            inference_load_timeout_seconds=_float(env, "SURVEY_INFERENCE_LOAD_TIMEOUT_SECONDS", 600.0),
            inference_retries=_int(env, "SURVEY_INFERENCE_RETRIES", 1, 0),
            http_timeout_seconds=_float(env, "SURVEY_HTTP_TIMEOUT_SECONDS", 10.0),
            startup_wait_seconds=_float(env, "SURVEY_STARTUP_WAIT_SECONDS", 5.0),
            turn_deadline_seconds=_float(env, "SURVEY_TURN_DEADLINE_SECONDS", 6.0),
            db_connect_timeout_seconds=_float(env, "SURVEY_DB_CONNECT_TIMEOUT_SECONDS", 2.0),
            db_statement_timeout_seconds=_float(env, "SURVEY_DB_STATEMENT_TIMEOUT_SECONDS", 2.0),
            db_reconnect_backoff_seconds=_float(env, "SURVEY_DB_RECONNECT_BACKOFF_SECONDS", 1.0),
            extract_input_max_chars=_int(env, "SURVEY_EXTRACT_INPUT_MAX_CHARS", 2400, 0),
            socket_timeout_seconds=_float(env, "SURVEY_SOCKET_TIMEOUT_SECONDS", 15.0),
            max_connections=_int(env, "SURVEY_MAX_CONNECTIONS", 256, 1),
            max_sse_connections=_int(env, "SURVEY_MAX_SSE_CONNECTIONS", 50, 1),
            readyz_db_timeout_seconds=_float(env, "SURVEY_READYZ_DB_TIMEOUT_SECONDS", 2.0),
            release_retry_base_seconds=_float(env, "SURVEY_RELEASE_RETRY_BASE_SECONDS", 5.0),
            release_retry_max_seconds=_float(env, "SURVEY_RELEASE_RETRY_MAX_SECONDS", 60.0),
            release_retry_give_up_seconds=float(env.get("SURVEY_RELEASE_RETRY_GIVE_UP_SECONDS", "0")),
            turn_engine_version=env.get("SURVEY_TURN_ENGINE_VERSION", "0.4.3").lstrip("v"),   # 기본값 0.4.3: 헤드 기준(D109), 측정값 아님
            max_consecutive_reask=_int(env, "SURVEY_MAX_CONSECUTIVE_REASK", 3, 0),
            staff_call_retry_min_interval_seconds=_int(env, "SURVEY_STAFF_CALL_RETRY_MIN_INTERVAL_SECONDS", 30, 0),
            staff_call_retry_max_per_session=_int(env, "SURVEY_STAFF_CALL_RETRY_MAX_PER_SESSION", 10, 0),
            staff_call_report_max_per_session=_int(env, "SURVEY_STAFF_CALL_REPORT_MAX_PER_SESSION", 20, 0),
            release_poll_seconds=_float(env, "SURVEY_RELEASE_POLL_SECONDS", 5.0), keep_releases=_int(env, "SURVEY_KEEP_RELEASES", 2),
            export_interval_seconds=_float(env, "SURVEY_EXPORT_INTERVAL_SECONDS", 1.0),
            export_backoff_max_seconds=_float(env, "SURVEY_EXPORT_BACKOFF_MAX_SECONDS", 60.0),
            outbox_max_pending=_int(env, "SURVEY_OUTBOX_MAX_PENDING", 20000, 0),
            outbox_keep_sent_hours=_int(env, "SURVEY_OUTBOX_KEEP_SENT_HOURS", 24, 1),
            outbox_stage_grace_seconds=_float(env, "SURVEY_OUTBOX_STAGE_GRACE_SECONDS", 60.0), log_level=env.get("SURVEY_LOG_LEVEL", "INFO").upper())
        missing = [n for n, v in (("SURVEY_RELEASE_READER", s.release_reader), ("SURVEY_TRACE_INGEST", s.trace_ingest),
                                  ("SURVEY_MODEL_BACKEND", s.model_backend)) if not v]     # 계약은 이름의 존재만 요구한다. 빈 값은 설정이 아니다
        for name, spec, url in (("SURVEY_RELEASE_READER", s.release_reader, s.release_reader_url),
                                ("SURVEY_TRACE_INGEST", s.trace_ingest, s.trace_ingest_url)):
            if spec == "http" and not url:
                missing.append(name + "_URL")
        if s.risk_rules_file and s.risk_source:
            raise ConfigError("SURVEY_RISK_RULES_FILE과 SURVEY_RISK_SOURCE는 함께 쓸 수 없다(입력원은 하나)")
        if missing:
            raise ConfigError("필수 환경 변수가 없다: " + ", ".join(missing))
        return s


def load_factory(spec: str) -> Callable[[], Any]:
    """'모듈:함수' 문자열을 함수로. 'http'는 boundary_contracts의 HTTP 클라이언트를 만드는 기본 팩토리를 뜻한다
    (SURVEY_RELEASE_READER=http 이면 ...:http_release_reader, SURVEY_TRACE_INGEST=http 이면 ...:http_trace_ingest)."""
    if spec == "http":
        raise ConfigError("'http'는 읽는 쪽(서비스 조립)이 역할에 맞는 팩토리로 바꿔야 한다")
    if ":" not in spec:
        raise ConfigError(f"'모듈:함수' 형식이어야 한다: {spec!r}")
    mod, _, fn = spec.partition(":")
    try:
        return getattr(importlib.import_module(mod), fn)
    except (ImportError, AttributeError) as e:
        raise ConfigError(f"팩토리를 불러올 수 없다: {spec} ({type(e).__name__})") from e
