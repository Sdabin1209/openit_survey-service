"""릴리스 로더의 일시적 실패 재시도(추론 서버가 아직 뜨는 중 등)와 결정적 거부의 구분.

일시적(연결 실패, 502·503·504, 타임아웃, 적재 대기 시간 초과, I4 불능)은 포인터를 거부로 확정하지 않고 백오프로 같은 포인터를 다시 시도한다.
결정적(412·422·404·403, 승인 없음, 해시·버전 불일치)은 영구 거부+이전 릴리스 유지(D46)다.
"""
import json
import socket
import threading
import time
import urllib.request

import pytest
from boundary_contracts import BoundaryError

from survey_service.release import ReleaseLoader, ReleaseRegistry
from survey_service.remote import RemoteInference
from survey_service.service import App, serve
from survey_service.settings import Settings

from .http_server_support import start_server, server_port, stop_server
from .fakes import FakeReleaseReader, FakeTraceIngest, RecordingConsole, RecordingDispatcher
from .sample_bank import sample_bank
from .runtime_support import FlakyRuntime
from .test_inference_client import Env, TOKEN
from .test_release_loader import INSTALLED


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def with_clock(e, **kw):
    clock = Clock()
    e.loader = ReleaseLoader(e.reader, e.registry, e.client, workdir=e.loader.workdir, installed_versions=INSTALLED, clock=clock,
                             retry_base_seconds=5, retry_max_seconds=60, **kw)
    return clock


@pytest.fixture
def e(tmp_path):
    env = Env(tmp_path, timeout=1.0)
    yield env
    env.close()


UNAVAILABLE = BoundaryError("UNAVAILABLE", "엔진이 아직 뜨는 중")


def test_transient_failures_back_off_then_load_automatically(e):
    clock = with_clock(e)
    e.srv.put_fail_next = [UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE]
    delays = []
    for _ in range(6):
        r = e.loader.poll_once()
        assert r.action == "RETRY_LATER" and r.code == "REMOTE_UNAVAILABLE" and e.registry.active() is None
        rs = e.loader.status.retry
        delays.append(round(rs["_next_ts"] - clock.t))
        assert e.loader.status.rejections == [] and e.loader.status.last_pointer_version is None       # 거부로 확정하지 않았다
        # 백오프 안에서는 서버를 부르지 않는다
        puts = e.srv.puts
        assert e.loader.poll_once().action == "WAITING" and e.srv.puts == puts
        clock.t += delays[-1]
    assert delays == [5, 10, 20, 40, 60, 60]                                                 # 5초 시작, 최대 60초
    assert e.loader.status.retry["attempts"] == 6
    r = e.loader.poll_once()                                                                   # 서버가 준비됨
    assert r.action == "LOADED" and e.registry.active_id() == "rel-1"
    assert e.loader.status.retry is None and e.loader.status.last_pointer_version == 1 and e.loader.status.rejections == []
    assert e.loader.poll_once().action == "UNCHANGED"


def test_retry_status_is_visible_with_attempt_info(e):
    clock = with_clock(e)
    e.srv.put_fail_next = [UNAVAILABLE, UNAVAILABLE]
    e.loader.poll_once(); clock.t += 5; e.loader.poll_once()
    info = e.loader.retry_info()
    assert info["pointer_version"] == 1 and info["release_id"] == "rel-1" and info["attempts"] == 2
    assert "REMOTE_UNAVAILABLE" in info["last_error"] and info["last_attempt_at"].endswith("Z") and info["next_attempt_at"] > info["last_attempt_at"]
    assert info["first_failure_at"] <= info["last_attempt_at"] and not any(k.startswith("_") for k in info)


@pytest.mark.parametrize("response,code", [
    (BoundaryError("FAILED_PRECONDITION", "승인 없음·해시·설정 불일치"), "REMOTE_FAILED_PRECONDITION"),
    (BoundaryError("INTEGRITY_ERROR", "해시"), "REMOTE_INTEGRITY_ERROR"),
    (BoundaryError("UNSUPPORTED_VERSION", "계약 버전"), "REMOTE_UNSUPPORTED_VERSION"),
    (BoundaryError("NOT_FOUND", "릴리스 없음"), "REMOTE_NOT_FOUND"),
    (BoundaryError("PERMISSION_DENIED", "토큰"), "REMOTE_PERMISSION_DENIED"),
])
def test_deterministic_refusal_is_permanent_with_no_retry(e, response, code):
    clock = with_clock(e)
    e.srv.put_fail_next = [response]
    r = e.loader.poll_once()
    assert r.action == "REJECTED" and r.code == code and e.registry.active() is None
    assert e.loader.status.retry is None and e.loader.status.rejections[-1]["code"] == code
    puts = e.srv.puts
    clock.t += 1000
    assert e.loader.poll_once().action == "UNCHANGED" and e.srv.puts == puts                    # 다시 시도하지 않는다


def test_new_pointer_takes_priority_over_a_retrying_one(e):
    clock = with_clock(e)
    e.srv.put_fail_next = [UNAVAILABLE] * 3
    e.reader.add_release("rel-2", sample_bank(2), turn_engine_version=INSTALLED["turn_engine_version"],
                         model_runtime_version=INSTALLED["model_runtime_version"])
    e.srv.available["rel-2"] = "ok"
    e.loader.poll_once()
    assert e.loader.status.retry["release_id"] == "rel-1" and e.loader.poll_once().action == "WAITING"
    e.reader.set_pointer("rel-2")                                                               # 백오프가 끝나기 전에 새 포인터
    e.srv.put_fail_next.clear()
    r = e.loader.poll_once()
    assert r.action == "LOADED" and r.release_id == "rel-2" and e.registry.active_id() == "rel-2"
    assert e.loader.status.retry is None and e.loader.status.last_pointer_version == 2


def test_new_pointer_that_is_also_failing_restarts_the_retry_count(e):
    clock = with_clock(e)
    e.srv.put_fail_next = [UNAVAILABLE] * 5
    e.reader.add_release("rel-2", sample_bank(2), turn_engine_version=INSTALLED["turn_engine_version"],
                         model_runtime_version=INSTALLED["model_runtime_version"])
    e.srv.available["rel-2"] = "ok"
    e.loader.poll_once(); clock.t += 5; e.loader.poll_once()
    assert e.loader.status.retry["attempts"] == 2
    e.reader.set_pointer("rel-2")
    e.loader.poll_once()
    rs = e.loader.status.retry
    assert rs["pointer_version"] == 2 and rs["release_id"] == "rel-2" and rs["attempts"] == 1


def test_slow_load_that_outlasts_the_wait_succeeds_on_a_later_try(tmp_path):
    e = Env(tmp_path, load_delay=0.6, load_timeout=0.25)
    try:
        clock = with_clock(e)
        r = e.loader.poll_once()
        assert r.action == "RETRY_LATER" and r.code == "REMOTE_TIMEOUT"                          # 서버는 아직 LOADING
        time.sleep(0.7)                                                                          # 서버가 다 올렸다
        clock.t += 5
        r = e.loader.poll_once()
        assert r.action == "LOADED" and e.registry.active_id() == "rel-1" and e.loader.status.retry is None
    finally:
        e.close()


def test_slow_load_within_the_wait_is_just_waited_for(tmp_path):
    e = Env(tmp_path, load_delay=0.5, load_timeout=5)
    try:
        with_clock(e)
        t0 = time.monotonic()
        assert e.loader.poll_once().action == "LOADED" and time.monotonic() - t0 >= 0.45 and e.loader.status.retry is None
    finally:
        e.close()


def test_server_failed_state_that_is_transient_retries_and_deterministic_does_not(tmp_path):
    e = Env(tmp_path, load_error=BoundaryError("UNAVAILABLE", "GPU"))
    try:
        clock = with_clock(e)
        assert e.loader.poll_once().action == "RETRY_LATER"
        e.srv.load_error = None; e.srv.load_delay = 0; e.srv._load_started.clear(); clock.t += 5          # 서버가 회복했다
        assert e.loader.poll_once().action == "LOADED"
    finally:
        e.close()
    e2 = Env(tmp_path / "x", load_error=BoundaryError("FAILED_PRECONDITION", "x"))
    try:
        with_clock(e2)
        r = e2.loader.poll_once()
        assert r.action == "REJECTED" and r.code == "REMOTE_FAILED_PRECONDITION"
    finally:
        e2.close()


def test_connection_refused_is_transient_until_the_server_comes_up(tmp_path):
    e = Env(tmp_path, timeout=0.5)
    port = e.srv._server.server_address[1]
    e.srv.stop()                                                                                # 추론 서버가 아직 안 떴다
    clock = with_clock(e)
    try:
        r = e.loader.poll_once()
        assert r.action == "RETRY_LATER" and r.code == "REMOTE_UNAVAILABLE" and e.loader.status.rejections == []
        e.srv = FlakyRuntime({TOKEN: "survey-service"}).start(port)                              # 같은 주소에 서버가 뜬다
        clock.t += 5
        assert e.loader.poll_once().action == "LOADED" and e.registry.active_id() == "rel-1"
    finally:
        e.close()


def test_i4_unavailable_also_backs_off_without_marking_the_pointer(e):
    clock = with_clock(e)
    e.reader.errors["get_release"] = [BoundaryError("UNAVAILABLE", "x")] * 2
    assert e.loader.poll_once().action == "RETRY_LATER" and e.loader.poll_once().action == "WAITING"
    clock.t += 5
    assert e.loader.poll_once().action == "RETRY_LATER" and e.loader.status.retry["attempts"] == 2
    clock.t += 10
    assert e.loader.poll_once().action == "LOADED" and e.loader.status.retry is None


def test_give_up_limit_turns_into_a_recorded_rejection(e):
    clock = with_clock(e, give_up_seconds=30)
    e.srv.put_fail_next = [UNAVAILABLE] * 20
    for _ in range(10):
        r = e.loader.poll_once()
        if r.action == "REJECTED":
            break
        assert r.action == "RETRY_LATER"
        clock.t = e.loader.status.retry["_next_ts"]                                              # 다음 시도 시각까지
    assert r.action == "REJECTED" and r.code == "RETRY_EXHAUSTED" and e.loader.status.retry is None
    assert e.loader.status.rejections[-1]["code"] == "RETRY_EXHAUSTED"
    assert e.loader.poll_once().action == "UNCHANGED"
    # 상한이 없으면(기본) 아무리 오래 걸려도 계속 다시 시도한다
    e2_clock = with_clock(e)
    e.srv.put_fail_next = [UNAVAILABLE] * 5
    for _ in range(5):
        assert e.loader.poll_once().action == "RETRY_LATER"
        e2_clock.t += 100000


# ---------------------------------------------------------------- 서비스 수준: /readyz, /healthz
def http(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as ex:
        return ex.code, json.loads(ex.read())


def free_port():
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    return port


def test_service_stays_alive_and_becomes_ready_when_the_inference_server_finishes_starting(tmp_path, monkeypatch):
    gate = FakeReleaseReader(tmp_path / "gate")
    gate.add_release("rel-1", sample_bank(), turn_engine_version=INSTALLED["turn_engine_version"],
                     model_runtime_version=INSTALLED["model_runtime_version"])
    gate.set_pointer("rel-1")
    port = free_port()                                                                           # 추론 서버가 아직 안 떴다: 연결 거부 -> 이어서 503 -> 정상
    inf = FlakyRuntime({TOKEN: "survey-service"})
    monkeypatch.setenv("SURVEY_API_TOKEN", "a-0123456789abcdef"); monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "s-0123456789abcdef")
    monkeypatch.setenv("SURVEY_INFERENCE_TOKEN", TOKEN)
    s = Settings.from_env({"SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"), "SURVEY_WORK_DIR": str(tmp_path / "w"),
                           "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_INFERENCE_URL": f"http://127.0.0.1:{port}",
                           "SURVEY_INFERENCE_TIMEOUT_SECONDS": "0.5", "SURVEY_RELEASE_POLL_SECONDS": "0.05",
                           "SURVEY_RELEASE_RETRY_BASE_SECONDS": "0.1", "SURVEY_RELEASE_RETRY_MAX_SECONDS": "0.3", "SURVEY_SESSION_IDLE_SECONDS": "0"})
    app = App(s, reader=gate, ingest=FakeTraceIngest(), staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
    app.start()
    front = serve(app, 0)
    start_server(front)
    fport = server_port(front)
    started = False
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (app.loader.retry_info() or {}).get("attempts", 0) >= 2:
            time.sleep(0.05)
        code, body = http(fport, "/readyz")
        assert code == 503 and body["problems"] == ["no_active_release", "release_loading"]
        rl = body["release_loader"]
        assert rl["rejections"] == [] and rl["retrying"]["release_id"] == "rel-1" and rl["retrying"]["attempts"] >= 2
        assert "REMOTE_UNAVAILABLE" in rl["retrying"]["last_error"]
        assert http(fport, "/healthz")[0] == 200                                                # 서비스는 살아 있다
        inf.put_fail_next = [UNAVAILABLE]                                                       # 서버가 뜨는 중: 첫 PUT은 503
        inf.start(port); started = True
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and http(fport, "/readyz")[0] != 200:
            time.sleep(0.1)
        code, body = http(fport, "/readyz")
        assert code == 200 and body["active_release"] == "rel-1" and body["problems"] == []
        assert body["release_loader"]["retrying"] is None and body["release_loader"]["rejections"] == []
    finally:
        stop_server(front); app.stop()
        if started:
            inf.stop()


def test_previous_release_keeps_serving_while_a_new_one_is_retrying(tmp_path, monkeypatch):
    gate = FakeReleaseReader(tmp_path / "gate")
    for i in (1, 2):
        gate.add_release(f"rel-{i}", sample_bank(i), turn_engine_version=INSTALLED["turn_engine_version"],
                         model_runtime_version=INSTALLED["model_runtime_version"])
    gate.set_pointer("rel-1")
    e = Env(tmp_path / "e", timeout=0.5)
    e.srv.available["rel-2"] = "ok"
    try:
        clock = with_clock(e)
        e.loader.reader = gate
        assert e.loader.poll_once().action == "LOADED"
        e.srv.put_fail_next = [UNAVAILABLE] * 3
        gate.set_pointer("rel-2")
        assert e.loader.poll_once().action == "RETRY_LATER" and e.registry.active_id() == "rel-1"       # 이전 릴리스 유지
        clock.t += 5; e.loader.poll_once(); clock.t += 10; e.loader.poll_once()
        assert e.registry.active_id() == "rel-1" and e.loader.status.retry["attempts"] == 3
        clock.t += 20
        assert e.loader.poll_once().action == "LOADED" and e.registry.active_id() == "rel-2"
    finally:
        e.close()


def test_start_does_not_block_healthz_while_the_model_is_loading(tmp_path, monkeypatch):
    """GPU 모델 적재에 수 분이 걸려도 서비스는 열려 있고 /healthz는 200이다. 끝나면 스스로 ready가 된다."""
    gate = FakeReleaseReader(tmp_path / "gate")
    gate.add_release("rel-1", sample_bank(), turn_engine_version=INSTALLED["turn_engine_version"],
                     model_runtime_version=INSTALLED["model_runtime_version"])
    gate.set_pointer("rel-1")
    srv = FlakyRuntime({TOKEN: "survey-service"}, load_delay=1.5).start()
    monkeypatch.setenv("SURVEY_API_TOKEN", "a-0123456789abcdef"); monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "s-0123456789abcdef"); monkeypatch.setenv("SURVEY_INFERENCE_TOKEN", TOKEN)
    s = Settings.from_env({"SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"), "SURVEY_WORK_DIR": str(tmp_path / "w"),
                           "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_INFERENCE_URL": srv.url,
                           "SURVEY_INFERENCE_LOAD_TIMEOUT_SECONDS": "30", "SURVEY_STARTUP_WAIT_SECONDS": "0.3", "SURVEY_SESSION_IDLE_SECONDS": "0"})
    app = App(s, reader=gate, ingest=FakeTraceIngest(), staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
    front = None
    try:
        t0 = time.monotonic()
        app.start()
        assert time.monotonic() - t0 < 1.2                                                           # 적재를 끝까지 기다리지 않았다
        front = serve(app, 0)
        start_server(front)
        port = server_port(front)
        assert http(port, "/healthz")[0] == 200
        assert http(port, "/readyz")[0] == 503
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and http(port, "/readyz")[0] != 200:
            time.sleep(0.1)
        assert http(port, "/readyz")[0] == 200
    finally:
        if front:
            stop_server(front)
        app.stop(); srv.stop()
