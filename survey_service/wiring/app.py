"""서비스 조립과 HTTP 서버(/healthz, /readyz, 공개 API 연결, 직원 알림 SSE). 공개 API 라우팅은 api.py(D55), 설정은 settings.py."""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from typing import Callable
from pathlib import Path

import model_runtime
import survey_schema
from boundary_contracts import PIPELINE_SCHEMA_VERSION, SecretError, read_secret, secret_source, validator
from boundary_contracts.action_rules import load_rules

from .. import SERVICE_VERSION
from ..components.alert_service import AlertHub, AlertService, AlertStaffConsole
from ..components.link_push import WebPushSender, endpoint_syntax_error
from ..components.link_public_api import PublicApi
from ..components.link_inference import InferenceServer, RemoteInference
from .. import types
from ..config import REAL_SLEEP, REAL_WALL_CLOCK, real_wait
from . import checked
from .clock import join_until
from .checked import effective_reask_cap
from .orchestrator import Orchestrator
from ..components.field_specs import invalid_field_specs
from ..components.release_loader import ReleaseLoader
from ..components.release_registry import ReleaseRegistry
from ..components.risk_screen import RuleRiskSource
from ..settings import ConfigError, Settings, load_factory
from .db import open_stores
from ..components.trace_envelope import OutboxTraceSink
from ..components.link_trace_export import TraceExporter

log = logging.getLogger("survey_service")


_REQ = threading.local()                  # 요청마다 요청 ID(X-Request-Id 또는 새로 만든 값). 로그 한 줄마다 request_id 필드로 붙는다 (감사 E1)
_REQ_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class JsonFormatter(logging.Formatter):
    """로그는 표준 출력에 JSON 한 줄. 발화·모델 출력·Trace 내용은 어디서도 로그로 내지 않는다."""
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z", "level": record.levelname,
             "logger": record.name, "msg": record.getMessage()}
        rid = getattr(_REQ, "rid", None)
        if rid:
            d["request_id"] = rid
        return json.dumps(d, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


def _spec(spec: str, which: str) -> str:
    """'http'는 boundary_contracts의 HTTP 클라이언트 기본 팩토리(survey_service.components.link_boundary_clients)."""
    if spec == "http":
        return "survey_service.components.link_boundary_clients:http_release_reader" if which == "release" else "survey_service.components.link_boundary_clients:http_trace_ingest"
    return spec


def _secret(name: str, what: str):
    """비밀 하나를 NAME_FILE 우선, 없으면 NAME 환경 변수에서 읽는다(D84). 오류 메시지에 값은 없다."""
    try:
        return read_secret(name)
    except SecretError as e:
        raise ConfigError(f"{what}: {e}") from None


class App:
    """저장소·릴리스 로더·Trace 내보내기·Orchestrator를 조립한다. reader/ingest를 주면 팩토리 설정을 쓰지 않는다(시험)."""

    def __init__(self, settings: Settings, *, reader=None, ingest=None, staff_console=None, dispatcher=None, risk_source=None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._readyz_validator = validator("SurveyReadyz")      # 기동 때 한 번: validator()는 호출마다 계약 스키마 파일을 다시 읽고 검사해, 요청마다 만들면 /readyz 2초 마감 뒤에 그 비용이 붙는다(E2E 5pn, ADR-0050)
        self.clock = clock          # 주입한 단조 시계(ADR-0050): 턴 마감·/readyz 마감·추론 재시도 예산을 이 시계로 센다. 기본은 실제 시계
        self.settings = settings
        Path(settings.work_dir).mkdir(parents=True, exist_ok=True)
        self.store, self.outbox, self.alert_store, self.db_kind = open_stores(settings)
        self.outbox.max_pending = settings.outbox_max_pending
        push = None
        vapid_key = _secret(settings.vapid_private_key_env, "VAPID 비밀키") or ""
        if vapid_key:                                       # 키가 없으면 푸시 없이 SSE만 동작한다
            if not settings.vapid_subject:
                raise ConfigError("VAPID 비밀키가 있는데 SURVEY_VAPID_SUBJECT(mailto: 또는 https://)가 없다")
            try:
                push = WebPushSender(vapid_key, settings.vapid_subject, settings.push_timeout_seconds, allow_private_hosts=settings.push_allow_private_hosts)
            except ValueError as e:
                raise ConfigError(str(e)) from None
        self.alerts = checked.wrap_alert_service(AlertService(checked.wrap_alert_store(self.alert_store), AlertHub(), push, settings.push_timeout_seconds,
                                    settings.staff_call_retry_min_interval_seconds, settings.staff_call_retry_max_per_session,
                                    settings.staff_call_report_max_per_session, endpoint_check=endpoint_syntax_error, monotonic=clock))
        self.registry = ReleaseRegistry()
        if settings.inference_url:                        # 모델 런타임을 HTTP 경계 뒤에(추론 컨테이너)
            tok = _secret(settings.inference_token_env, "추론 서버 토큰")
            if not tok:
                raise ConfigError(f"추론 서버 토큰이 {settings.inference_token_env}_FILE(파일) 또는 환경 변수 {settings.inference_token_env}에 없다")
            self.inference = RemoteInference(settings.inference_url, tok, settings.inference_timeout_seconds,
                                         load_timeout=settings.inference_load_timeout_seconds, retries=settings.inference_retries,
                                         extract_timeout=settings.inference_extract_timeout_seconds,
                                         respond_timeout=settings.inference_respond_timeout_seconds, monotonic=clock, sleep=REAL_SLEEP)
        else:
            self.inference = InferenceServer(settings.model_backend)
        self.reader = reader if reader is not None else load_factory(_spec(settings.release_reader, 'release'))()
        self.ingest = ingest if ingest is not None else load_factory(_spec(settings.trace_ingest, 'trace'))()
        self.loader = ReleaseLoader(
            self.reader, self.registry, self.inference, workdir=Path(settings.work_dir), keep_releases=settings.keep_releases,
            installed_versions={"action_rules_version": load_rules()["rules_version"], "model_runtime_version": model_runtime.RUNTIME_VERSION},
            open_release_ids=self.store.open_release_ids, retry_base_seconds=settings.release_retry_base_seconds,
            retry_max_seconds=settings.release_retry_max_seconds, give_up_seconds=settings.release_retry_give_up_seconds,
            field_check=invalid_field_specs, clock=REAL_WALL_CLOCK)
        self.exporter = TraceExporter(self.outbox, self.ingest, base_delay=settings.export_interval_seconds,
                                      max_delay=settings.export_backoff_max_seconds, stage_grace=settings.outbox_stage_grace_seconds,
                                      clock=REAL_WALL_CLOCK, wait=real_wait)
        console = staff_console if staff_console is not None else (load_factory(settings.staff_console)() if settings.staff_console else None)
        disp = dispatcher if dispatcher is not None else (load_factory(settings.result_dispatcher)() if settings.result_dispatcher else None)
        if console is None:                                   # 기본 직원 접점은 알림 어댑터. SURVEY_STAFF_CONSOLE로 바꿔 꽂을 수 있다
            console = AlertStaffConsole(self.alerts)
        if settings.risk_rules_file and settings.risk_source:
            raise ConfigError("SURVEY_RISK_RULES_FILE과 SURVEY_RISK_SOURCE는 함께 쓸 수 없다(입력원은 하나)")
        if risk_source is None and settings.risk_rules_file:
            risk_source = RuleRiskSource.from_file(settings.risk_rules_file)        # 오류는 ConfigError: 시작을 거부한다
            d = risk_source.describe()
            log.info("위험 규칙 적재 version=%s rules=%s sha256=%s", d["version"], d["rule_count"], d["sha256"])
        elif risk_source is None and settings.risk_source:
            risk_source = load_factory(settings.risk_source)()
        elif risk_source is None and settings.risk_default_rules:
            risk_source = RuleRiskSource.provisional_default()                        # 임상 검토 전 임시 기본 목록. 입력원이 꺼진 채 시작하지 않는다 (D88 U2, ADR-0013)
            log.warning("위험 감지: 임상 검토 전 임시 기본 목록을 쓴다 version=%s (SURVEY_RISK_RULES_FILE로 병원 목록을 준다)", risk_source.version)
        self.orchestrator = Orchestrator(self.store, checked.wrap_registry(self.registry), risk_source=risk_source, staff_console=console, dispatcher=disp,
                                         trace_sink=OutboxTraceSink(self.outbox, REAL_WALL_CLOCK), timeline=self.alert_store,
                                         max_consecutive_reask=settings.max_consecutive_reask,
                                         turn_deadline_seconds=settings.turn_deadline_seconds, request_deadline_seconds=settings.turn_deadline_seconds, extract_input_max_chars=settings.extract_input_max_chars, clock=clock)
        api_token = _secret(settings.api_token_env, "공개 API 토큰")
        device_tokens = _device_tokens(settings.kiosk_device_tokens_env)
        staff_token = _secret(settings.staff_api_token_env, "직원 개입 토큰")
        if not staff_token:
            raise ConfigError(f"직원 개입 토큰이 {settings.staff_api_token_env}_FILE(파일) 또는 환경 변수 {settings.staff_api_token_env}에 없다")
        if api_token and staff_token == api_token:
            raise ConfigError("직원 개입 토큰은 키오스크 토큰과 달라야 한다")
        _check_token_length(api_token or None, "공개 API 토큰")
        _check_token_length(staff_token, "직원 개입 토큰")
        result_token = _secret(settings.result_reader_token_env, "결과 조회 토큰") or None     # 없으면 해당 API가 꺼진다
        risk_token = _secret(settings.risk_source_token_env, "위험 신호 토큰") or None
        _check_token_length(result_token, "결과 조회 토큰")
        _check_token_length(risk_token, "위험 신호 토큰")
        if (result_token is not None and result_token in (api_token, staff_token)) or (risk_token is not None and risk_token in (api_token, staff_token)) or (result_token and result_token == risk_token) or \
                len({staff_token, result_token, risk_token, *device_tokens.values()} - {None}) != len([x for x in (staff_token, result_token, risk_token, *device_tokens.values()) if x is not None]) or (api_token and api_token in device_tokens.values()):
            raise ConfigError("토큰은 역할마다 서로 달라야 한다")
        self.api = PublicApi(self.orchestrator, api_token or None, device_tokens=device_tokens, staff_token=staff_token, alerts=self.alerts, max_subscriptions=settings.max_push_subscriptions,
                             result_token=result_token, risk_token=risk_token, require_subject_ref=settings.require_subject_ref, push_allow_private=settings.push_allow_private_hosts,
                             retry_after_seconds=settings.retry_after_seconds, state_poll_min_interval=settings.state_poll_min_interval_seconds,
                             max_inflight=settings.api_max_inflight,
                             max_utterance_chars=settings.api_max_utterance_chars)
        self._stop = threading.Event()
        self.last_retention: dict = {}
        self.draining = threading.Event()        # SIGTERM을 받았다: /readyz는 503(shutting_down), 처리 중 요청은 끝낸다
        self._threads: list = []

    # ------------------------------------------------------------------ 수명
    def start(self) -> None:
        self.exporter.start(self.settings.export_interval_seconds)
        first_done = threading.Event()

        def poll():
            n = 0
            first = True
            # 첫 폴링은 바로 한다(첫 활성 릴리스, 다시 띄운 뒤 진행 중 세션의 릴리스). 추론 서버가 아직 뜨는 중이면 오래 걸릴 수 있어
            # 이 스레드에서 하고, start()는 startup_wait_seconds까지만 기다린다(그동안에도 /healthz는 산다).
            while first or not self._stop.wait(self.settings.release_poll_seconds):
                try:
                    self.loader.poll_once()
                    if first:
                        self.loader.ensure_loaded(self.store.open_release_ids())
                    elif self.loader.status.unavailable_releases:
                        self.loader.ensure_loaded(self.store.open_release_ids())
                    n += 1
                    if n % 60 == 0:
                        self.outbox.purge_sent(time.time() - self.settings.outbox_keep_sent_hours * 3600)
                        self.alert_store.trim_events(keep=10000)      # SSE 이어 받기 범위(최근 1만 건) 밖의 이벤트를 지운다. 알림 행은 남는다
                except Exception as e:
                    log.error("릴리스 폴링 실패 error=%s", type(e).__name__)
                if first:
                    first = False
                    first_done.set()
        t = threading.Thread(target=poll, name="release-poller", daemon=True)
        t.start()
        self._threads.append(t)
        first_done.wait(self.settings.startup_wait_seconds)
        st = self.settings
        log.info("보존 기간(일) session=%s access_log=%s alerts=%s dead_outbox=%s (잠정 기본값, 병원 규정에 맞게 설정)",
                 st.session_retention_days, st.access_log_retention_days, st.alert_retention_days, st.dead_outbox_retention_days)
        if st.retention_interval_seconds > 0:
            def retention_loop():
                from ..components.retention_purge import purge
                while not self._stop.wait(st.retention_interval_seconds):
                    try:
                        self.last_retention = purge(self.store, self.alert_store, st.session_retention_days, REAL_WALL_CLOCK(), outbox=self.outbox,
                                                    access_log_days=st.access_log_retention_days, alert_days=st.alert_retention_days,
                                                    dead_outbox_days=st.dead_outbox_retention_days)
                        self.last_retention["at"] = time.time()
                    except Exception as e:
                        log.error("보존 정리 실패 error=%s", type(e).__name__)
            t3 = threading.Thread(target=retention_loop, name="retention", daemon=True)
            t3.start()
            self._threads.append(t3)
        if self.settings.session_idle_seconds > 0:
            idle = self.settings.session_idle_seconds

            def sweep():
                while not self._stop.wait(max(1.0, min(30.0, idle / 4))):
                    try:
                        self.api.expire_idle(self.store.list_idle(time.time() - idle))
                    except Exception as e:
                        log.error("유휴 만료 주기 실패 error=%s", type(e).__name__)
            t2 = threading.Thread(target=sweep, name="idle-sweeper", daemon=True)
            t2.start()
            self._threads.append(t2)

    def stop(self) -> None:
        self._stop.set()
        self.exporter.stop()
        for t in self._threads:
            t.join(timeout=5)
        self.store.close()
        self.outbox.close()
        self.alert_store.close()

    # ------------------------------------------------------------------ 상태
    def _secret_sources(self) -> dict:
        """비밀 출처만(file|env|unset). 값·파일 경로·변수 이름은 내지 않는다(D84)."""
        st = self.settings
        names = {"api_token": st.api_token_env, "kiosk_device_tokens": st.kiosk_device_tokens_env, "staff_api_token": st.staff_api_token_env, "vapid_private_key": st.vapid_private_key_env,
                 "result_reader_token": st.result_reader_token_env, "risk_source_token": st.risk_source_token_env}
        if st.inference_url:
            names["inference_token"] = st.inference_token_env
        if st.release_reader == "http":
            names["release_reader_token"] = st.release_reader_token_env
        if st.trace_ingest == "http":
            names["trace_ingest_token"] = st.trace_ingest_token_env
        return {k: secret_source(v) for k, v in names.items()}

    def _bounded(self, fns: dict, budget: float) -> dict:
        """함수들을 데몬 스레드에서 동시에 돌리고 `budget`초 안에 끝난 것만 {이름: ("ok", 값) | ("error", None)}로 돌려준다(못 끝낸 것은 빠진다).
        /readyz가 DB 호출 하나 때문에 오래 붙잡히지 않게 하는 마지막 상한이다."""
        out: dict = {}

        def run(name, fn):
            try:
                out[name] = ("ok", fn())
            except Exception:
                out[name] = ("error", None)
        end = self.clock() + budget          # 마감은 스레드를 시작하기 전에 정한다
        ts = [threading.Thread(target=run, args=(n, f), name="readyz-" + n, daemon=True) for n, f in fns.items()]
        for t in ts:
            t.start()
        for t in ts:
            join_until(t, end, self.clock)
        return dict(out)

    def _db_check(self) -> tuple[list, dict]:
        """세 저장소 확인과 보조 수치 읽기를 **동시에** 시작하고 **한 번의 총 마감 `readyz_db_timeout_seconds`(기본 2초)** 안에 끝난 것만 쓴다.
        근거: 계약 FAULT_POLICY 5는 의존이 응답하지 않으면 /readyz가 2초 안에 503을 내라고 한다(E2E 단언 허용 2.5초). 이전 구현은 확인(총 2초)이 끝난 뒤
        보조 수치(주 연결)를 1초 더 **이어서** 기다려 최악 3초였다: 네트워크 분리 직후 확인용 새 연결은 통과하고 주 연결 읽기만 매달린 요청이 생기면
        그다음 요청까지 합쳐 분리부터 503까지 약 3초(gpu-dev E2E 5pn 2.8초 관찰과 맞는 경로, ADR-0027). 이제 마감은 응답 전체에 하나다.
        각 확인은 자기 사유(session_db/outbox_db/alert_db)를 따로 보고한다. 보조 수치는 확인이 모두 통과했을 때만 쓰고(주 연결이 정상일 때의 값),
        마감 안에 못 읽으면 마지막 값에 stale=true를 붙인다. 마감을 넘긴 스레드는 데몬이라 버려진다(PostgreSQL 확인은 자체 connect_timeout으로 끝난다)."""
        out: dict = {}

        def run(name, fn):
            try:
                out[name] = ("ok", fn())
            except Exception:
                out[name] = ("error", None)
        fns = {"session_db": self.store.ping, "outbox_db": self.outbox.ping, "alert_db": self.alert_store.ping,
               "stats": lambda: {"trace_outbox": self.exporter.stats(), "staff_alerts": self.alerts.status()}}
        end = self.clock() + self.settings.readyz_db_timeout_seconds          # 마감은 스레드를 시작하기 전에 정한다
        ts = {n: threading.Thread(target=run, args=(n, f), name="readyz-" + n, daemon=True) for n, f in fns.items()}
        for th in ts.values():
            th.start()
        for n in ("session_db", "outbox_db", "alert_db"):
            join_until(ts[n], end, self.clock)
        # 왜: 확인이 끝난 뒤 보조 수치를 마감까지 기다리면, 분리 직후(새 연결은 통과, 주 연결은 매달림) 200 응답이 2초를 다 쓰고 다음 요청의 503이 그만큼 늦는다.
        #     확인 뒤에는 STATS_GRACE_S만 더 기다린다(총 마감 안). 값은 측정하지 않았다(ADR-0027): 정상 보조 수치는 로컬 질의 몇 ms라 넉넉하다는 판단이다.
        join_until(ts["stats"], min(end, self.clock() + STATS_GRACE_S), self.clock)
        res = dict(out)
        problems = [n for n in ("session_db", "outbox_db", "alert_db") if res.get(n, ("error",))[0] != "ok"]
        if not problems and res.get("stats", ("error",))[0] == "ok":
            self._db_stats_cache = res["stats"][1]
            return problems, dict(self._db_stats_cache, stale=False)
        c = getattr(self, "_db_stats_cache", None)
        # 보조 수치를 한 번도 읽지 못했다: 계약 SurveyReadyz는 trace_outbox·staff_alerts를 객체로 요구한다(null이면 계약 위반이라 R7 검증이 막는다). 0으로 채우고 db_stats_stale=true로 값이 없음을 알린다
        empty_outbox = {"pending": 0, "sent": 0, "dead": 0, "dropped_full": 0, "dead_by_code": {}, "oldest_pending_age_seconds": None, "consecutive_unavailable": 0, "last_error": None}
        return problems, (dict(c, stale=True) if c else {"trace_outbox": empty_outbox, "staff_alerts": {"delivery_failed": 0}, "stale": True})

    def _db_problems(self) -> list:
        """세 저장소 확인 결과만(시험·진단용). /readyz는 `_db_check`를 쓴다."""
        return self._db_check()[0]

    def _reask_limit(self) -> dict:
        """되묻기 상한(계약 SurveyReadyz.reask_limit, 3.1.0). effective는 활성 문항 세트까지 반영한 실제 적용 값이고, 활성 릴리스가 없거나 상한이 꺼져 있으면(0) null이다
        (계약: effective는 1 이상 또는 null). 이웃은 규칙을 흉내 내지 않고 이 값만 읽는다(ADR-0046)."""
        mx = self.settings.max_consecutive_reask
        rel = self.registry.active()
        cap = effective_reask_cap(mx, rel.bank.get("session_settings") if rel else None) if rel else None
        return {"active": (cap if cap is not None else mx) > 0, "max_consecutive": mx, "effective": cap or None}

    def readiness(self) -> tuple[bool, dict]:
        problems, dbs = self._db_check()
        if self.draining.is_set():
            problems.append("shutting_down")
        active = self.registry.active_id()
        retrying = self.loader.retry_info()
        if active is None:
            problems.append("no_active_release")
            if retrying is not None:
                problems.append("release_loading")      # 거부가 아니라 일시적 실패로 재시도 중이다(release_loader.retrying)
        body = {
            "ready": not problems, "problems": problems, "database": ({"kind": self.db_kind} if not any(p.endswith("_db") for p in problems) else
                         {"kind": self.db_kind, "status": "UNAVAILABLE", "detail": "db unavailable"}), "service_version": SERVICE_VERSION,
            "active_release": active, "contract_version": survey_schema.current_version(),
            "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
            "staff_alerts": dbs["staff_alerts"],
            "secrets": self._secret_sources(),
            # 입력원이 없으면(기본 임시 목록을 SURVEY_RISK_DEFAULT_RULES=off로 끈 경우만) 조용히 끄지 않고 여기에 active=false로 드러낸다(ADR-0013, CR-012).
            "staff_alert_source": ((self.orchestrator.risk_source.describe() if hasattr(self.orchestrator.risk_source, "describe")
                                    else {"active": True}) if self.orchestrator.risk_source is not None else
                                   {"active": False, "reason": "NO_INPUT_SOURCE_IN_V2_EXTRACTION",
                                    "detail": "ActionExtraction에는 위험 신호·직원 호출 필드가 없다. 알림 저장·전달 경로는 살아 있으나 입력원이 꺼져 알림이 만들어지지 않는다(SURVEY_RISK_DEFAULT_RULES=off, 병원 규칙 파일·입력원 없음; docs/change-requests/CR-012)."}),
            "validation_violations": self.orchestrator.violation_stats(),
            "result_dispatcher": self.orchestrator.dispatcher_status(),
            "retention": {"session_days": self.settings.session_retention_days, "access_log_days": self.settings.access_log_retention_days,
                          "alert_days": self.settings.alert_retention_days, "dead_outbox_days": self.settings.dead_outbox_retention_days,
                          "indefinite": False, "provisional_defaults": True, "last_run": self.last_retention or None},
            "kiosk_tokens": {"shared_kiosk_token": bool(self.api.token), "device_tokens": len(self.api.device_tokens),
                             "warning": ("공유 키오스크 토큰 사용 중: 기기별 토큰으로 바꾸는 것을 권고(ADR-0016)" if self.api.token else None)},
            "api": {**{"idle_expiry_seconds": self.settings.session_idle_seconds}, **self.api.api_stats()},
            "reask_limit": self._reask_limit(),
            "response_tables": {"status": self.orchestrator.tables.status, "counters": dict(self.orchestrator.tables.stats)},
            "question_flow": {"flow_id": self.orchestrator.flow.flow_id, "items": len(self.orchestrator.flow.table.order)},
            "inference": (self.inference.stats.snapshot() if hasattr(self.inference, "stats") else None),
            "model_backend": self.inference.backend, "model_backend_production_safe": self.inference.production_safe,
            "release_loader": {"retrying": retrying, "unavailable_releases": self.loader.status.unavailable_releases,
                               "last_pointer_version": self.loader.status.last_pointer_version,
                               "last_poll_error": self.loader.status.last_poll_error,
                               "rejections": self.loader.status.rejections[-5:], "warnings": self.loader.status.warnings[-5:]},
            "trace_outbox": dbs["trace_outbox"], "db_stats_stale": dbs["stale"],
        }
        # R7: 만드는 쪽이 내보내기 전에 계약 SurveyReadyz로 검증한다(openit_link.App의 ready는 스키마를 보지 않는다). 어기면 값은 싣지 않고 위반 위치만 담아 준비 안 됨(503)으로 낸다
        errs = sorted({"/".join(str(x) for x in e.absolute_path) or "(최상위)" for e in self._readyz_validator.iter_errors(body)})
        if errs:
            log.error("/readyz 본문이 계약 SurveyReadyz를 어긴다 paths=%s", errs[:5])
            return False, {"ready": False, "problems": ["readyz_contract_violation"], "violation_paths": errs[:10]}
        return not problems, body


MIN_TOKEN_CHARS = 16      # 기기 토큰과 역할 토큰의 최소 길이. 근거: ADR-0016(기기 토큰 16자)과 같은 규칙을 역할 토큰에도 적용한다(결정표 2026-10-29, ADR-0024). 16은 측정·표준 값이 아니다


def _check_token_length(token, what: str) -> None:
    if token is not None and len(token) < MIN_TOKEN_CHARS:
        raise ConfigError(f"{what}이 너무 짧다: {MIN_TOKEN_CHARS}자 이상이어야 한다")     # 값은 오류에 넣지 않는다


STATS_GRACE_S = 0.2       # /readyz: DB 확인이 끝난 뒤 보조 수치(주 연결)를 더 기다리는 상한(초). 측정하지 않은 값, ADR-0027


def _device_tokens(env_name: str) -> dict:
    """기기별 키오스크 토큰 표 {device_id: 토큰}. `read_secret(NAME)`(NAME_FILE 우선)의 내용이 줄마다(또는 쉼표로 구분된) `device_id=토큰`. 비밀 값은 어디에도 출력하지 않는다 (D88 U8)."""
    try:
        raw = read_secret(env_name) or ""
    except SecretError as e:
        raise ConfigError(f"기기별 키오스크 토큰: {e}") from None
    out: dict = {}
    for item in raw.replace(",", "\n").splitlines():
        item = item.strip()
        if not item or item.startswith("#"):
            continue
        dev, sep, tok = item.partition("=")
        dev, tok = dev.strip(), tok.strip()
        if not sep or not re.match(r"^[A-Za-z0-9._-]{1,64}$", dev) or len(tok) < MIN_TOKEN_CHARS:
            raise ConfigError("기기별 키오스크 토큰 표 형식 오류: 줄마다 `device_id=토큰`(토큰 16자 이상)")
        if dev in out:
            raise ConfigError("기기별 키오스크 토큰 표에 같은 device_id가 두 번 있다")
        out[dev] = tok
    return out
