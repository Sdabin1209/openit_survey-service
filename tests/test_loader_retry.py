"""릴리스 로더의 일시적 실패 재시도(추론 서버가 아직 뜨는 중 등)와 결정적 거부의 구분.

일시적(연결 실패, 502·503·504, 타임아웃, 적재 대기 시간 초과, I4 불능)은 포인터를 거부로 확정하지 않고 백오프로 같은 포인터를 다시 시도한다.
결정적(412·422·404·403, 승인 없음, 해시·버전 불일치)은 영구 거부+이전 릴리스 유지(D46)다.
"""
import json
import threading
import time
import urllib.request

import pytest
from boundary_contracts import BoundaryError

from survey_service.release import ReleaseLoader, ReleaseRegistry
from survey_service.remote import RemoteInference
from survey_service.service import App, serve
from survey_service.settings import Settings

from .fakes import FakeReleaseReader, FakeTraceIngest, RecordingConsole, RecordingDispatcher
from .ref_inference_server import build, err, make_server
from .sample_bank import sample_bank
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


UNAVAILABLE = err("UNAVAILABLE", "엔진이 아직 뜨는 중")


def test_transient_failures_back_off_then_load_automatically(e):
    clock = with_clock(e)
    e.inf.put_fail_next = [UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE]
    delays = []
    for _ in range(6):
        r = e.loader.poll_once()
        assert r.action == "RETRY_LATER" and r.code == "REMOTE_UNAVAILABLE" and e.registry.active() is None
        rs = e.loader.status.retry
        delays.append(round(rs["_next_ts"] - clock.t))
        assert e.loader.status.rejections == [] and e.loader.status.last_pointer_version is None       # 거부로 확정하지 않았다
        # 백오프 안에서는 서버를 부르지 않는다
        puts = e.inf.puts
        assert e.loader.poll_once().action == "WAITING" and e.inf.puts == puts
        clock.t += delays[-1]
    assert delays == [5, 10, 20, 40, 60, 60]                                                 # 5초 시작, 최대 60초
    assert e.loader.status.retry["attempts"] == 6
    r = e.loader.poll_once()                                                                   # 서버가 준비됨
    assert r.action == "LOADED" and e.registry.active_id() == "rel-1"
    assert e.loader.status.retry is None and e.loader.status.last_pointer_version == 1 and e.loader.status.rejections == []
    assert e.loader.poll_once().action == "UNCHANGED"


def test_retry_status_is_visible_with_attempt_info(e):
    clock = with_clock(e)
    e.inf.put_fail_next = [UNAVAILABLE, UNAVAILABLE]
    e.loader.poll_once(); clock.t += 5; e.loader.poll_once()
    info = e.loader.retry_info()
    assert info["pointer_version"] == 1 and info["release_id"] == "rel-1" and info["attempts"] == 2
    assert "REMOTE_UNAVAILABLE" in info["last_error"] and info["last_attempt_at"].endswith("Z") and info["next_attempt_at"] > info["last_attempt_at"]
    assert info["first_failure_at"] <= info["last_attempt_at"] and not any(k.startswith("_") for k in info)


@pytest.mark.parametrize("response,code", [
    (err("FAILED_PRECONDITION", "승인 없음·해시·설정 불일치"), "REMOTE_FAILED_PRECONDITION"),
    (err("INTEGRITY_ERROR", "해시"), "REMOTE_INTEGRITY_ERROR"),
    (err("UNSUPPORTED_VERSION", "계약 버전"), "REMOTE_UNSUPPORTED_VERSION"),
    (err("NOT_FOUND", "릴리스 없음"), "REMOTE_NOT_FOUND"),
    (err("PERMISSION_DENIED", "토큰"), "REMOTE_PERMISSION_DENIED"),
])
def test_deterministic_refusal_is_permanent_with_no_retry(e, response, code):
    clock = with_clock(e)
    e.inf.put_fail_next = [response]
    r = e.loader.poll_once()
    assert r.action == "REJECTED" and r.code == code and e.registry.active() is None
    assert e.loader.status.retry is None and e.loader.status.rejections[-1]["code"] == code
    puts = e.inf.puts
    clock.t += 1000
    assert e.loader.poll_once().action == "UNCHANGED" and e.inf.puts == puts                    # 다시 시도하지 않는다


def test_new_pointer_takes_priority_over_a_retrying_one(e):
    clock = with_clock(e)
    e.inf.put_fail_next = [UNAVAILABLE] * 3
    e.reader.add_release("rel-2", sample_bank(2), turn_engine_version=INSTALLED["turn_engine_version"],
                         model_runtime_version=INSTALLED["model_runtime_version"])
    e.loader.poll_once()
    assert e.loader.status.retry["release_id"] == "rel-1" and e.loader.poll_once().action == "WAITING"
    e.reader.set_pointer("rel-2")                                                               # 백오프가 끝나기 전에 새 포인터
    e.inf.put_fail_next.clear()
    r = e.loader.poll_once()
    assert r.action == "LOADED" and r.release_id == "rel-2" and e.registry.active_id() == "rel-2"
    assert e.loader.status.retry is None and e.loader.status.last_pointer_version == 2


def test_new_pointer_that_is_also_failing_restarts_the_retry_count(e):
    clock = with_clock(e)
    e.inf.put_fail_next = [UNAVAILABLE] * 5
    e.reader.add_release("rel-2", sample_bank(2), turn_engine_version=INSTALLED["turn_engine_version"],
                         model_runtime_version=INSTALLED["model_runtime_version"])
    e.loader.poll_once(); clock.t += 5; e.loader.poll_once()
    assert e.loader.status.retry["attempts"] == 2
    e.reader.set_pointer("rel-2")
    e.loader.poll_once()
    rs = e.loader.status.retry
    assert rs["pointer_version"] == 2 and rs["release_id"] == "rel-2" and rs["attempts"] == 1


def test_slow_load_that_outlasts_the_wait_succeeds_on_a_later_try(tmp_path):
    e = Env(tmp_path, async_load=True, load_delay=0.6, load_timeout=0.25)
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
    e = Env(tmp_path, async_load=True, load_delay=0.5, load_timeout=5)
    try:
        with_clock(e)
        t0 = time.time()
        assert e.loader.poll_once().action == "LOADED" and time.time() - t0 >= 0.45 and e.loader.status.retry is None
    finally:
        e.close()


def test_server_failed_state_that_is_transient_retries_and_deterministic_does_not(tmp_path):
    e = Env(tmp_path, async_load=True)
    try:
        clock = with_clock(e)
        orig = e.inf._load
        def fail_unavailable(rid):
            e.inf._state[rid] = {"state": "FAILED", "error": {"code": "UNAVAILABLE", "message": "GPU", "retryable": True}}
        e.inf._load = fail_unavailable
        assert e.loader.poll_once().action == "RETRY_LATER"
        e.inf._state.clear(); e.inf._load = orig; clock.t += 5
        assert e.loader.poll_once().action == "LOADED"
    finally:
        e.close()
    e2 = Env(tmp_path / "x", async_load=True)
    try:
        with_clock(e2)
        e2.inf._load = lambda rid: e2.inf._state.__setitem__(rid, {"state": "FAILED", "error": {"code": "FAILED_PRECONDITION", "message": "x", "retryable": False}})
        r = e2.loader.poll_once()
        assert r.action == "REJECTED" and r.code == "REMOTE_FAILED_PRECONDITION"
    finally:
        e2.close()


def test_connection_refused_is_transient_until_the_server_comes_up(tmp_path):
    e = Env(tmp_path, timeout=0.5)
    port = e.httpd.server_address[1]
    e.httpd.shutdown(); e.httpd.server_close()                                                  # 추론 서버가 아직 안 떴다
    clock = with_clock(e)
    try:
        r = e.loader.poll_once()
        assert r.action == "RETRY_LATER" and r.code == "REMOTE_UNAVAILABLE" and e.loader.status.rejections == []
        httpd = make_server(e.inf, port)                                                        # 같은 주소에 서버가 뜬다
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        e.httpd = httpd
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
    e.inf.put_fail_next = [UNAVAILABLE] * 20
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
    e.inf.put_fail_next = [UNAVAILABLE] * 5
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


def test_service_stays_alive_and_becomes_ready_when_the_inference_server_finishes_starting(tmp_path, monkeypatch):
    gate = FakeReleaseReader(tmp_path / "gate")
    gate.add_release("rel-1", sample_bank(), turn_engine_version=INSTALLED["turn_engine_version"],
                     model_runtime_version=INSTALLED["model_runtime_version"])
    gate.set_pointer("rel-1")
    # 추론 서버: 포트만 먼저 잡아 두고 아직 뜨지 않은 척한다(연결 거부 -> 이어서 503 -> 정상)
    probe = make_server(build(Settings(work_dir=str(tmp_path / "iw"), release_reader="x:y", trace_ingest="x:y", model_backend="fake"),
                              reader=gate, token=TOKEN), 0)
    port = probe.server_address[1]
    probe.server_close()
    inf = build(Settings(work_dir=str(tmp_path / "iw2"), release_reader="x:y", trace_ingest="x:y", model_backend="fake"), reader=gate, token=TOKEN)
    monkeypatch.setenv("SURVEY_API_TOKEN", "a"); monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "s")
    monkeypatch.setenv("SURVEY_INFERENCE_TOKEN", TOKEN)
    s = Settings.from_env({"SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"), "SURVEY_WORK_DIR": str(tmp_path / "w"),
                           "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_INFERENCE_URL": f"http://127.0.0.1:{port}",
                           "SURVEY_INFERENCE_TIMEOUT_SECONDS": "0.5", "SURVEY_RELEASE_POLL_SECONDS": "0.05",
                           "SURVEY_RELEASE_RETRY_BASE_SECONDS": "0.1", "SURVEY_RELEASE_RETRY_MAX_SECONDS": "0.3", "SURVEY_SESSION_IDLE_SECONDS": "0"})
    app = App(s, reader=gate, ingest=FakeTraceIngest(), staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
    app.start()
    front = serve(app, 0)
    threading.Thread(target=front.serve_forever, daemon=True).start()
    fport = front.server_address[1]
    srv = None
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not (app.loader.retry_info() or {}).get("attempts", 0) >= 2:
            time.sleep(0.05)
        code, body = http(fport, "/readyz")
        assert code == 503 and body["problems"] == ["no_active_release", "release_loading"]
        rl = body["release_loader"]
        assert rl["rejections"] == [] and rl["retrying"]["release_id"] == "rel-1" and rl["retrying"]["attempts"] >= 2
        assert "REMOTE_UNAVAILABLE" in rl["retrying"]["last_error"]
        assert http(fport, "/healthz")[0] == 200                                                # 서비스는 살아 있다
        inf.put_fail_next = [UNAVAILABLE]                                                       # 서버가 뜨는 중: 첫 PUT은 503
        srv = make_server(inf, port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        deadline = time.time() + 8
        while time.time() < deadline and http(fport, "/readyz")[0] != 200:
            time.sleep(0.1)
        code, body = http(fport, "/readyz")
        assert code == 200 and body["active_release"] == "rel-1" and body["problems"] == []
        assert body["release_loader"]["retrying"] is None and body["release_loader"]["rejections"] == []
    finally:
        front.shutdown(); front.server_close(); app.stop()
        if srv:
            srv.shutdown(); srv.server_close()


def test_previous_release_keeps_serving_while_a_new_one_is_retrying(tmp_path, monkeypatch):
    gate = FakeReleaseReader(tmp_path / "gate")
    for i in (1, 2):
        gate.add_release(f"rel-{i}", sample_bank(i), turn_engine_version=INSTALLED["turn_engine_version"],
                         model_runtime_version=INSTALLED["model_runtime_version"])
    gate.set_pointer("rel-1")
    e = Env(tmp_path / "e", timeout=0.5)
    try:
        clock = with_clock(e)
        e.loader.reader = gate
        e.inf.loader.reader = gate
        assert e.loader.poll_once().action == "LOADED"
        e.inf.put_fail_next = [UNAVAILABLE] * 3
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
    inf = build(Settings(work_dir=str(tmp_path / "iw"), release_reader="x:y", trace_ingest="x:y", model_backend="fake"),
                reader=gate, token=TOKEN, async_load=True, load_delay=1.5)
    srv = make_server(inf, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("SURVEY_API_TOKEN", "a"); monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "s"); monkeypatch.setenv("SURVEY_INFERENCE_TOKEN", TOKEN)
    s = Settings.from_env({"SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"), "SURVEY_WORK_DIR": str(tmp_path / "w"),
                           "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_INFERENCE_URL": f"http://127.0.0.1:{srv.server_address[1]}",
                           "SURVEY_INFERENCE_LOAD_TIMEOUT_SECONDS": "30", "SURVEY_STARTUP_WAIT_SECONDS": "0.3", "SURVEY_SESSION_IDLE_SECONDS": "0"})
    app = App(s, reader=gate, ingest=FakeTraceIngest(), staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
    front = None
    try:
        t0 = time.time()
        app.start()
        assert time.time() - t0 < 1.2                                                           # 적재를 끝까지 기다리지 않았다
        front = serve(app, 0)
        threading.Thread(target=front.serve_forever, daemon=True).start()
        port = front.server_address[1]
        assert http(port, "/healthz")[0] == 200
        assert http(port, "/readyz")[0] == 503
        deadline = time.time() + 8
        while time.time() < deadline and http(port, "/readyz")[0] != 200:
            time.sleep(0.1)
        assert http(port, "/readyz")[0] == 200
    finally:
        if front:
            front.shutdown(); front.server_close()
        app.stop(); srv.shutdown(); srv.server_close()
