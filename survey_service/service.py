"""서비스 조립과 HTTP 서버(/healthz, /readyz, 공개 API 연결, 직원 알림 SSE). 공개 API 라우팅은 api.py(D55), 설정은 settings.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
import math
import os
import queue
import re
import signal
import sys
import threading
import time
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Optional

import model_runtime
import survey_schema
from boundary_contracts import PIPELINE_SCHEMA_VERSION, SecretError, read_secret, secret_source
from boundary_contracts.action_rules import load_rules
from waitress import create_server
from waitress import wasyncore

from . import SERVICE_VERSION
from .alerts import AlertHub, AlertService, AlertStaffConsole, AlertStore, WebPushSender
from .api import PublicApi, _err
from .inference import InferenceServer
from .remote import RemoteInference
from .graph import effective_reask_cap
from .orchestrator import Orchestrator
from .release import ReleaseLoader, ReleaseRegistry
from .risk_rules import RuleRiskSource
from .errors import StoreError
from .settings import ConfigError, Settings, load_factory
from .db import open_stores
from .trace import Outbox, OutboxTraceSink, TraceExporter

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
    """'http'는 boundary_contracts의 HTTP 클라이언트 기본 팩토리(survey_service.clients)."""
    if spec == "http":
        return "survey_service.clients:http_release_reader" if which == "release" else "survey_service.clients:http_trace_ingest"
    return spec


def _secret(name: str, what: str):
    """비밀 하나를 NAME_FILE 우선, 없으면 NAME 환경 변수에서 읽는다(D84). 오류 메시지에 값은 없다."""
    try:
        return read_secret(name)
    except SecretError as e:
        raise ConfigError(f"{what}: {e}") from None


class App:
    """저장소·릴리스 로더·Trace 내보내기·Orchestrator를 조립한다. reader/ingest를 주면 팩토리 설정을 쓰지 않는다(시험)."""

    def __init__(self, settings: Settings, *, reader=None, ingest=None, staff_console=None, dispatcher=None, risk_source=None) -> None:
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
        self.alerts = AlertService(self.alert_store, AlertHub(), push, settings.push_timeout_seconds,
                                    settings.staff_call_retry_min_interval_seconds, settings.staff_call_retry_max_per_session,
                                    settings.staff_call_report_max_per_session)
        self.registry = ReleaseRegistry()
        if settings.inference_url:                        # 모델 런타임을 HTTP 경계 뒤에(추론 컨테이너)
            tok = _secret(settings.inference_token_env, "추론 서버 토큰")
            if not tok:
                raise ConfigError(f"추론 서버 토큰이 {settings.inference_token_env}_FILE(파일) 또는 환경 변수 {settings.inference_token_env}에 없다")
            self.inference = RemoteInference(settings.inference_url, tok, settings.inference_timeout_seconds,
                                         load_timeout=settings.inference_load_timeout_seconds, retries=settings.inference_retries,
                                         extract_timeout=settings.inference_extract_timeout_seconds,
                                         respond_timeout=settings.inference_respond_timeout_seconds)
        else:
            self.inference = InferenceServer(settings.model_backend)
        self.reader = reader if reader is not None else load_factory(_spec(settings.release_reader, 'release'))()
        self.ingest = ingest if ingest is not None else load_factory(_spec(settings.trace_ingest, 'trace'))()
        self.loader = ReleaseLoader(
            self.reader, self.registry, self.inference, workdir=Path(settings.work_dir), keep_releases=settings.keep_releases,
            installed_versions={"action_rules_version": load_rules()["rules_version"], "model_runtime_version": model_runtime.RUNTIME_VERSION},
            open_release_ids=self.store.open_release_ids, retry_base_seconds=settings.release_retry_base_seconds,
            retry_max_seconds=settings.release_retry_max_seconds, give_up_seconds=settings.release_retry_give_up_seconds)
        self.exporter = TraceExporter(self.outbox, self.ingest, base_delay=settings.export_interval_seconds,
                                      max_delay=settings.export_backoff_max_seconds, stage_grace=settings.outbox_stage_grace_seconds)
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
        self.orchestrator = Orchestrator(self.store, self.registry, risk_source=risk_source, staff_console=console, dispatcher=disp,
                                         trace_sink=OutboxTraceSink(self.outbox), timeline=self.alert_store,
                                         max_consecutive_reask=settings.max_consecutive_reask,
                                         turn_deadline_seconds=settings.turn_deadline_seconds, extract_input_max_chars=settings.extract_input_max_chars)
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
                from .retention import purge
                while not self._stop.wait(st.retention_interval_seconds):
                    try:
                        self.last_retention = purge(self.store, self.alert_store, st.session_retention_days, outbox=self.outbox,
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
        ts = [threading.Thread(target=run, args=(n, f), name="readyz-" + n, daemon=True) for n, f in fns.items()]
        for t in ts:
            t.start()
        end = time.monotonic() + budget
        for t in ts:
            t.join(max(0.0, end - time.monotonic()))
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
        ts = {n: threading.Thread(target=run, args=(n, f), name="readyz-" + n, daemon=True) for n, f in fns.items()}
        for th in ts.values():
            th.start()
        end = time.monotonic() + self.settings.readyz_db_timeout_seconds
        for n in ("session_db", "outbox_db", "alert_db"):
            ts[n].join(max(0.0, end - time.monotonic()))
        # 왜: 확인이 끝난 뒤 보조 수치를 마감까지 기다리면, 분리 직후(새 연결은 통과, 주 연결은 매달림) 200 응답이 2초를 다 쓰고 다음 요청의 503이 그만큼 늦는다.
        #     확인 뒤에는 STATS_GRACE_S만 더 기다린다(총 마감 안). 값은 측정하지 않았다(ADR-0027): 정상 보조 수치는 로컬 질의 몇 ms라 넉넉하다는 판단이다.
        ts["stats"].join(max(0.0, min(end, time.monotonic() + STATS_GRACE_S) - time.monotonic()))
        res = dict(out)
        problems = [n for n in ("session_db", "outbox_db", "alert_db") if res.get(n, ("error",))[0] != "ok"]
        if not problems and res.get("stats", ("error",))[0] == "ok":
            self._db_stats_cache = res["stats"][1]
            return problems, dict(self._db_stats_cache, stale=False)
        c = getattr(self, "_db_stats_cache", None)
        return problems, (dict(c, stale=True) if c else {"trace_outbox": None, "staff_alerts": None, "stale": True})

    def _db_problems(self) -> list:
        """세 저장소 확인 결과만(시험·진단용). /readyz는 `_db_check`를 쓴다."""
        return self._db_check()[0]

    def _reask_limit(self) -> dict:
        """되묻기 상한. effective는 활성 문항 세트까지 반영한 실제 적용 값(0=끔, 활성 릴리스가 없으면 null). 이웃은 규칙을 흉내 내지 않고 이 값을 읽는다(ADR-0046)."""
        mx = self.settings.max_consecutive_reask
        rel = self.registry.active()
        eff = effective_reask_cap(mx, rel.bank.get("session_settings") if rel else None) if rel else None
        return {"active": (eff if eff is not None else mx) > 0, "max_consecutive": mx, "effective": eff}

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


_WAITRESS_INTERNAL_FDS = 2     # waitress는 connection_limit에 듣기 소켓과 깨우기(trigger) 소켓을 함께 센다: 측정(limit=4일 때 동시 연결 2개만 응답)으로 확인, 그만큼 더해야 설정이 클라이언트 연결 수가 된다 (ADR-0029)
_BODY_LIMIT = 1_000_000          # 요청 본문 상한(바이트). waitress max_request_body_size로 적용되고 넘으면 waitress가 413을 낸다 (ADR-0029)


def _status_line(code: int) -> str:
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return f"{code} Unknown"


class _SseBody:
    """SSE 응답 본문(WSGI iterable). waitress는 클라이언트가 끊으면 다음 쓰기에서 반복을 멈추고 close()를 부른다: 구독을 거기서 푼다."""

    def __init__(self, app: App, q, sent: int, hub) -> None:
        self._app, self._q, self._sent, self._hub, self._closed = app, q, sent, hub, False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._hub.unsubscribe(self._q)

    def __iter__(self):
        app, q, sent = self._app, self._q, self._sent
        try:
            last_ping = time.monotonic()
            while not app._stop.is_set() and not app.draining.is_set() and not getattr(q, "overflowed", False):
                try:
                    ev = q.get(timeout=min(app.settings.sse_heartbeat_seconds, 0.5))      # 종료 신호를 0.5초 안에 본다(우아한 종료가 스트림을 기다리지 않게)
                except queue.Empty:
                    if time.monotonic() - last_ping >= app.settings.sse_heartbeat_seconds:
                        yield _heartbeat()
                        last_ping = time.monotonic()
                    continue
                if ev["id"] > sent:                          # 이어 받기 구간과 겹치는 이벤트는 건너뛴다
                    yield _sse(ev)
                    sent = ev["id"]
        except StoreError:                                 # 알림 DB 오류: 스트림을 끝낸다(클라이언트는 Last-Event-ID로 다시 연결한다)
            log.warning("알림 스트림을 DB 오류로 끝낸다")
        finally:
            self.close()


def _heartbeat() -> bytes:
    """주석 줄(`: ping`, 프록시·연결 유지용)과 이름 있는 이벤트 `heartbeat`(브라우저 EventSource가 앱 코드에서 볼 수 있다, id 없음 = Last-Event-ID를 바꾸지 않는다).
    DB를 읽지 않는다: 알림 DB·상류가 느리거나 죽어도 스트림은 하트비트를 계속 보낸다. 기존 클라이언트는 모르는 이벤트 이름을 무시한다(CR-016)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return b": ping\n\nevent: heartbeat\ndata: " + json.dumps({"ts": ts}).encode() + b"\n\n"


def _sse(ev: dict) -> bytes:
    return f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(ev['alert'], ensure_ascii=False)}\n\n".encode()


def make_wsgi(app: App):
    """WSGI(PEP 3333) 애플리케이션. 서버(소켓, 스레드, 접속·시간 제한)는 waitress가 맡고 이 함수는 경로 분기와 응답 모양만 안다 (ADR-0029)."""

    def application(environ, start_response):
        method = environ["REQUEST_METHOD"]
        raw_path = environ.get("RAW_URI") or environ.get("REQUEST_URI") or environ.get("PATH_INFO", "/")
        path = raw_path.split("?", 1)[0]
        hdr_in = lambda name: environ.get("HTTP_" + name.upper().replace("-", "_"), "")
        rid = hdr_in("X-Request-Id").strip()
        _REQ.rid = rid if _REQ_ID.match(rid) else uuid.uuid4().hex[:16]

        def respond(code: int, body: Optional[dict], extra: Optional[dict] = None):
            headers = [("X-Request-Id", _REQ.rid)]
            if code == 204:      # 왜: 204는 본문이 없어야 한다(RFC 9110 15.3.5). 본문과 Content-Type을 보내지 않는다
                start_response(_status_line(204), headers + list((extra or {}).items()))
                return [b""]
            raw = json.dumps(body, ensure_ascii=False).encode()
            headers += [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(raw)))] + list((extra or {}).items())
            start_response(_status_line(code), headers)
            return [raw]

        if method == "GET" and path == "/v1/staff/alerts/stream":
            return stream(environ, respond, start_response, hdr_in)
        if path.startswith("/v1/") and method in ("GET", "POST", "DELETE"):
            try:
                n = int(environ.get("CONTENT_LENGTH") or 0)
                if n < 0:
                    raise ValueError
            except ValueError:                                     # waitress가 먼저 걸러 보통 오지 않는다. 오면 400
                return respond(400, {"pipeline_schema_version": PIPELINE_SCHEMA_VERSION, "code": "INVALID_RECORD",
                                     "message": "Content-Length가 올바르지 않다", "retryable": False})
            raw = environ["wsgi.input"].read(n) if n > 0 else b""
            code, body, hdr = app.api.handle(method, raw_path, hdr_in("Authorization"), raw, user=hdr_in("X-Openit-User").strip() or None,
                                             device=environ.get("HTTP_X_OPENIT_DEVICE"))
            return respond(code, body, hdr)
        if method == "GET" and path == "/healthz":
            return respond(200, {"status": "ok"})
        if method == "GET" and path == "/readyz":
            ok, body = app.readiness()
            return respond(200 if ok else 503, body)
        return respond(404, {"error": "not found"})            # 공개 API 밖의 경로·메서드는 없다

    def stream(environ, respond, start_response, hdr_in):
        """GET /v1/staff/alerts/stream: SSE. Last-Event-ID가 있으면 그 뒤 이벤트부터 이어 받는다."""
        err = app.api.stream_auth_error(hdr_in("Authorization"))
        if err:
            code, body, hdr = err
            return respond(code, body, hdr)
        hub, store = app.alerts.hub, app.alert_store
        if hub.count() >= app.settings.max_sse_connections:      # 스트림은 연결마다 작업 스레드를 쥔다: 상한을 넘으면 503
            return respond(*_err("UNAVAILABLE", "알림 스트림 연결이 너무 많다", 503, {"Retry-After": str(app.settings.retry_after_seconds)}))
        last = hdr_in("Last-Event-ID").strip()
        q = hub.subscribe()                               # 먼저 구독하고 나서 저장된 이벤트를 읽는다(빠지는 구간이 없게)
        try:
            try:
                latest, oldest = store.latest_event_id(), store.oldest_event_id()
            except StoreError:                             # 알림 DB가 죽어 있으면 200 스트림을 열지 않고 503(클라이언트는 Retry-After 뒤 Last-Event-ID로 다시 연결한다)
                hub.unsubscribe(q)
                return respond(*_err("UNAVAILABLE", "알림 저장소를 읽을 수 없다", 503, {"Retry-After": str(app.settings.retry_after_seconds)}))
            first = [b"retry: 3000\n\n", _heartbeat()]           # 연결 직후에도 한 번(콘솔이 '연결됨'을 확인할 수 있게)
            sent = -1
            if last.isdigit():                               # id는 이벤트 순번. 끊긴 동안의 이벤트를 이어서 보낸다
                sent = int(last)
                if sent > latest or (oldest is not None and sent < oldest - 1):      # 이어 받을 수 없다(오래된 이벤트가 지워졌거나 순번이 앞섰다): 목록 API로 다시 맞추게 알린다
                    first.append(b'event: resync\ndata: {"reason":"EVENTS_NOT_RESUMABLE"}\n\n')
                for ev in store.events_after(sent):
                    first.append(_sse(ev))
                    sent = ev["id"]
        except StoreError:
            hub.unsubscribe(q)
            return respond(*_err("UNAVAILABLE", "알림 저장소를 읽을 수 없다", 503, {"Retry-After": str(app.settings.retry_after_seconds)}))
        start_response("200 OK", [("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-cache"), ("X-Accel-Buffering", "no")])
        body = _SseBody(app, q, sent, hub)
        return _chain(first, body)

    return application


class _chain:
    """앞에 보낼 조각들을 먼저 내고 본문 iterable을 잇는다. close()는 본문에 전달한다."""

    def __init__(self, first, body) -> None:
        self._first, self._body = first, body

    def __iter__(self):
        yield from self._first
        yield from self._body

    def close(self) -> None:
        self._body.close()


def serve(app: App, port: int):
    """waitress 서버(단일 프로세스·다중 스레드)를 만든다. `.run()`이 블록하고 `close_server()`로 닫는다.
    값의 출처는 docs/adr/ADR-0029.md: 작업 스레드는 처리 중 요청 상한 + SSE 상한 + 여유(스트림이 요청 하나가 끝날 때까지 스레드를 쥐므로),
    연결 상한·읽기 시간 제한은 기존 설정을 그대로(측정 안 함), backlog는 이전 값 128(부하 측정 N=50 근거)."""
    st = app.settings
    return create_server(
        make_wsgi(app), host="0.0.0.0", port=port, ident="survey-service", threads=st.api_max_inflight + st.max_sse_connections + 4,
        connection_limit=st.max_connections + _WAITRESS_INTERNAL_FDS, channel_timeout=max(1, math.ceil(st.socket_timeout_seconds)), cleanup_interval=1,
        max_request_body_size=_BODY_LIMIT, backlog=128, expose_tracebacks=False)


def close_server(server, wait_seconds: float) -> None:
    """우아한 종료: 새 접속을 받지 않고, 처리 중인 요청이 끝나기를 기다린 뒤(최대 wait_seconds) 남은 연결을 닫는다. 호출 전에 app.draining을 세워 SSE가 끝나게 한다."""
    if getattr(server, "_survey_closed", False):
        return
    server._survey_closed = True
    server.close()
    dispatcher = server.task_dispatcher
    end = time.monotonic() + wait_seconds
    while dispatcher.active_count and time.monotonic() < end:
        time.sleep(0.05)
    dispatcher.shutdown(cancel_pending=True, timeout=1)
    wasyncore.close_all(server._map)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["purge"]:
        from .retention import run_purge
        return run_purge(argv=argv[1:])
    if argv:
        print("알 수 없는 명령: 서버는 인자 없이, 보존 정리는 `purge`", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    try:
        app = App(settings)
    except Exception as e:
        log.error("시작 실패 error=%s message=%s", type(e).__name__, e if isinstance(e, (ConfigError, StoreError)) else "")
        return 3
    app.start()
    httpd = serve(app, settings.port)
    log.info("시작 port=%s version=%s", settings.port, SERVICE_VERSION)
    def on_term(*_):
        app.draining.set()                      # /readyz가 먼저 503이 되어 로드 밸런서가 새 요청을 보내지 않게 한다
        threading.Thread(target=close_server, args=(httpd, settings.turn_deadline_seconds + 1.0), daemon=True).start()
    signal.signal(signal.SIGTERM, on_term)
    try:
        httpd.run()
    except KeyboardInterrupt:
        pass
    finally:
        close_server(httpd, 0.0)
        app.stop()
    return 0
