"""추론 서버 호출(survey_service/remote.py): 계약의 RuntimeReferenceServer(tests/runtime_support.py의 사고 주입 확장)로 HTTP 경계를 시험한다.

느린 서버, 죽은 서버, 틀린 출력, 503 OUTPUT_INVALID(D75), 릴리스를 잊은 서버를 서비스가 어떻게 다루는지 본다."""
import threading
import time

import pytest
from boundary_contracts import BoundaryError

from survey_service.orchestrator import Orchestrator
from survey_service.release import ReleaseLoader, ReleaseRegistry
from survey_service.remote import RemoteInference, Stats
from survey_service.store.memory import MemorySessionStore

from .fakes import FakeReleaseReader, RecordingConsole, RecordingDispatcher
from .runtime_support import FlakyRuntime, raw_response, redirect_extraction
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED

TOKEN = "inf-tok"


class Env:
    def __init__(self, tmp, *, timeout=0.5, retries=1, load_timeout=5.0, **srv_kw):
        self.reader = FakeReleaseReader(tmp / "gate")
        self.reader.add_release("rel-1", sample_bank(), turn_engine_version=INSTALLED["turn_engine_version"],
                                model_runtime_version=INSTALLED["model_runtime_version"])
        self.reader.set_pointer("rel-1")
        self.srv = FlakyRuntime({TOKEN: "survey-service"}, **srv_kw).start()
        self.url = self.srv.url
        self.registry = ReleaseRegistry()
        self.client = RemoteInference(self.url, TOKEN, timeout, load_timeout=load_timeout, retries=retries, poll_interval=0.05, backoff=0.01)
        self.loader = ReleaseLoader(self.reader, self.registry, self.client, workdir=tmp / "svc-work", installed_versions=INSTALLED, retry_base_seconds=0)
        self.orch = Orchestrator(MemorySessionStore(), self.registry, staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
        self.n = 0

    def turn(self, text="안녕하세요", sid="S1"):
        self.n += 1
        return self.orch.handle_turn(turn_id=f"t{self.n}", frontend_message={"session_id": sid, "kind": "UTTERANCE", "utterance": text})

    def start(self, sid="S1"):
        self.n += 1
        return self.orch.handle_turn(turn_id=f"t{self.n}", frontend_message={"session_id": sid, "kind": "SESSION_START"})

    def close(self):
        self.srv.stop()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    assert e.loader.poll_once().action == "LOADED"
    yield e
    e.close()


def failed(o):
    tr = o.orchestrator_output["log_entry"]["turn_result"]
    return tr["failed_steps"] if isinstance(tr, dict) else []


LORA_A_FAIL = [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]


def test_normal_turn_and_stats(env):
    env.start()
    o = env.turn()
    assert o.orchestrator_output["turn_failure_signal"] is None and o.frontend_input["response_text"] == "응답입니다."
    s = env.client.stats.snapshot()
    assert s["calls"] == 3 and s["failures"] == 0 and s["retries"] == 0
    assert env.srv.calls == ["respond", "extract", "respond"]


def test_slow_but_within_timeout_is_fine(env):
    env.srv.delay = 0.2
    o = env.start()
    assert o.frontend_input["response_text"] and o.orchestrator_output["turn_failure_signal"] is None


def test_slow_extract_beyond_timeout_fails_the_step_without_retry(env):
    env.start()
    env.srv.delay = 2.0
    t0 = time.time()
    o = env.turn()
    el = time.time() - t0
    assert 0.4 < el < 1.5                                            # 타임아웃(0.5s)에서 끊고 다시 시도하지 않는다
    assert failed(o) == LORA_A_FAIL
    assert o.orchestrator_output["turn_failure_signal"] == {"session_id": "S1", "write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}
    s = env.client.stats.snapshot()
    assert s["timeouts"] == 1 and s["retries"] == 0 and s["failures"] == 1


def test_slow_respond_fails_after_commit_state_is_kept(env):
    env.start()
    real = env.registry.active().session.respond

    def slow_respond(req):
        env.srv.delay = 2.0
        return real(req)
    env.registry.active().session.respond = slow_respond
    from survey_service.store.effects import answers_by_key
    env.srv.extract_fn = lambda req: {"action": "NEXT", "next_item": "Q1.D02", "values": [
        {"key": "Q1.D01.diagnosed", "value": True, "precision": "exact", "status": "ANSWERED", "save": True}]}
    o = env.turn()
    assert failed(o) == [{"phase": "AFTER_COMMIT", "step": "LoRA-B"}]
    assert o.orchestrator_output["turn_failure_signal"]["write_committed"] == "COMMITTED" and o.frontend_input["response_text"] is None
    assert answers_by_key(env.orch.store.load("S1")["state"]["answer_records"]) == {"Q1.D01.diagnosed": (True, "exact")}


def test_one_503_is_retried_and_succeeds(env):
    env.start()
    env.srv.fail_next = [BoundaryError("UNAVAILABLE", "과부하")]
    o = env.turn()
    assert failed(o) == [] and env.client.stats.snapshot()["retries"] == 1
    assert env.srv.calls[-3:] == ["extract", "extract", "respond"]


def test_persistent_503_fails_after_retries(env):
    env.start()
    env.srv.fail_next = [BoundaryError("UNAVAILABLE", "x")] * 2
    o = env.turn()
    assert failed(o) == LORA_A_FAIL
    s = env.client.stats.snapshot()
    assert s["retries"] == 1 and s["failures"] == 1


def test_retries_zero_means_single_attempt(tmp_path):
    e = Env(tmp_path, retries=0)
    try:
        e.loader.poll_once(); e.start()
        e.srv.fail_next = [BoundaryError("UNAVAILABLE", "x")]
        assert failed(e.turn()) == LORA_A_FAIL and e.client.stats.snapshot()["retries"] == 0
    finally:
        e.close()


def test_server_down_fails_fast_after_one_retry(env):
    env.start()
    env.close()
    t0 = time.time()
    o = env.turn()
    assert time.time() - t0 < 1.5 and failed(o) == LORA_A_FAIL
    assert env.client.stats.snapshot()["retries"] == 1


def test_400_from_server_is_not_retried(env):
    env.start()
    env.srv.fail_next = [BoundaryError("INVALID_RECORD", "입력이 계약을 어긴다")]
    o = env.turn()
    assert failed(o) == LORA_A_FAIL and env.client.stats.snapshot()["retries"] == 0


def test_output_invalid_503_is_retried_once_by_the_graph_and_succeeds(env):
    """D75: 서버가 끝내 형식을 못 지켜 503 OUTPUT_INVALID를 내면 서비스는 같은 요청을 한 번 더 보낸다."""
    env.start()
    seq = [{"action": "NOPE"}, redirect_extraction]
    env.srv.extract_fn = lambda req: (seq.pop(0) if isinstance(seq[0], dict) else seq.pop(0)(req))
    o = env.turn()
    assert failed(o) == [] and o.trace["extract_attempts"] == 2 and o.trace["extract_status"] == "OK"
    s = env.client.stats.snapshot()
    assert s["output_invalid"] == 1 and s["retries"] == 0                  # 클라이언트 재시도가 아니라 그래프의 재시도
    assert env.srv.calls[-3:] == ["extract", "extract", "respond"]


def test_output_invalid_twice_fails_the_turn_and_keeps_state(env):
    env.start()
    before = env.orch.store.load("S1")["state"]["progress_record"]
    env.srv.extract_fn = lambda req: {"action": "NOPE"}
    o = env.turn()
    assert failed(o) == LORA_A_FAIL and o.trace["extract_status"] == "OUTPUT_INVALID" and o.trace["extract_attempts"] == 2
    assert o.orchestrator_output["turn_failure_signal"] == {"session_id": "S1", "write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}
    assert env.orch.store.load("S1")["state"]["progress_record"] == before
    assert env.client.stats.snapshot()["output_invalid"] == 2


@pytest.mark.parametrize("bad", [
    {"values": "not a list"}, {"action": "NEXT"}, {"nothing": 1}, [], {"action": "NEXT", "next_item": "END", "values": [], "extra": 1},
])
def test_malformed_extract_response_fails_the_step_without_retry(env, bad):
    """서버가 형식을 어긴 200을 내면(서버 결함) 서비스가 대체 출력을 만들지 않고 그 단계를 실패시킨다."""
    env.start()
    env.srv.fail_next = [raw_response(200, bad, headers={"X-Openit-Outcome": "ok"})]
    o = env.turn()
    assert failed(o) == LORA_A_FAIL and env.client.stats.snapshot()["retries"] == 0


@pytest.mark.parametrize("body,ctype", [(b"", "text/plain"), (b"{\"response_text\": \"x\"}", "application/json")])
def test_malformed_respond_response_fails_after_commit(env, body, ctype):
    env.start()
    env.srv.calls.clear()
    real = env.srv._route

    def route(method, path, q, b, role):
        if path.endswith("/respond"):
            return 200, ctype, body, {"X-Openit-Outcome": "ok"}
        return real(method, path, q, b, role)
    env.srv._route = route
    o = env.turn()
    assert failed(o) == [{"phase": "AFTER_COMMIT", "step": "LoRA-B"}] and o.frontend_input["response_text"] is None


def test_non_json_body_is_a_failure(env):
    env.start()
    env.srv.fail_next = [raw_response(200, b"<html>gateway</html>", headers={"X-Openit-Outcome": "ok"})] * 2
    assert failed(env.turn()) == LORA_A_FAIL


def test_forgotten_release_is_reloaded_once(env):
    env.start()
    env.srv.forget()                                                    # 추론 컨테이너가 다시 떴다
    o = env.turn()
    assert failed(o) == [] and env.client.stats.snapshot()["reloads"] == 1


def test_forgotten_release_with_slow_reload_fails_the_turn_instead_of_waiting(tmp_path):
    e = Env(tmp_path, load_delay=1.0)
    try:
        assert e.loader.poll_once().action == "LOADED"                 # 적재 신호 뒤 끝날 때까지 기다린다
        e.start()
        e.srv.forget()
        t0 = time.time()
        o = e.turn()
        assert time.time() - t0 < 0.9 and failed(o) == LORA_A_FAIL
        time.sleep(1.3)                                                # 서버가 다 올리면 다음 턴은 성공
        assert failed(e.turn()) == []
    finally:
        e.close()


def test_async_load_is_polled_until_loaded(tmp_path):
    e = Env(tmp_path, load_delay=0.4)
    try:
        t0 = time.time()
        assert e.loader.poll_once().action == "LOADED" and time.time() - t0 >= 0.35
        assert e.registry.active_id() == "rel-1"
    finally:
        e.close()


def test_load_wait_timeout_is_transient_not_a_rejection(tmp_path):
    e = Env(tmp_path, load_delay=1.5, load_timeout=0.3)
    try:
        r = e.loader.poll_once()           # 서버가 아직 적재 중이다: 영구 거부가 아니라 재시도(tests/test_loader_retry.py)
        assert r.action == "RETRY_LATER" and r.code == "REMOTE_TIMEOUT" and e.registry.active() is None and not e.loader.status.rejections
    finally:
        e.close()


def test_server_side_load_failure_is_a_rejection(tmp_path):
    e = Env(tmp_path, load_error=BoundaryError("FAILED_PRECONDITION", "승인되지 않은 릴리스"))
    try:
        r = e.loader.poll_once()
        assert r.action == "REJECTED" and r.code == "REMOTE_FAILED_PRECONDITION" and e.registry.active() is None
    finally:
        e.close()


def test_other_sessions_are_not_blocked_by_slow_inference(tmp_path):
    e = Env(tmp_path, timeout=3.0)
    try:
        e.loader.poll_once()
        e.srv.delay = 0.3
        t0 = time.time()
        ts = [threading.Thread(target=lambda i=i: e.orch.handle_turn(turn_id="a", frontend_message={"session_id": f"P{i}", "kind": "SESSION_START"}))
              for i in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert time.time() - t0 < 0.9                                   # 직렬이면 4 x 0.3 = 1.2초 이상
    finally:
        e.close()


def test_server_side_output_protection_is_visible_and_turn_continues(tmp_path):
    """서버의 출력 보호(D36)로 대체 문장이 나가도(X-Openit-Outcome: fallback) 서비스는 턴을 실패시키지 않고 건수만 센다."""
    e = Env(tmp_path)
    try:
        e.loader.poll_once()
        e.srv.respond_fn = lambda req: ("잠시 후 다시 말씀해 주세요.", "fallback")
        e.srv.protections = "G0:1,G5:1"
        o = e.start()
        assert failed(o) == [] and o.frontend_input["response_text"] == "잠시 후 다시 말씀해 주세요."
        assert o.trace["response_outcome"] == "fallback" and o.trace["response_protections"] == {"G0": 1, "G5": 1}
        e.turn()
        snap = e.client.stats.snapshot()
        assert snap["outcome_fallback"] >= 2 and snap["protections"] == {"G0": 3, "G5": 3}      # 시작 1회 + 발화의 extract·respond
    finally:
        e.close()


def test_auth_failure_on_calls_fails_the_step(env):
    env.start()
    env.registry.active().session._rt._token = "wrong"
    o = env.turn()
    assert failed(o) == LORA_A_FAIL and env.client.stats.snapshot()["retries"] == 0


def test_protections_are_summed_per_rule():
    s = Stats()
    s.add_protections({"G0": 2, "G5": 1})
    s.add_protections({"G0": 1})
    assert s.snapshot()["protections"] == {"G0": 3, "G5": 1}
