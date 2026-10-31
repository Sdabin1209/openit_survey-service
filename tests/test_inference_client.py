"""추론 서버 호출(survey_service/remote.py): 계약의 RuntimeReferenceServer(tests/runtime_support.py의 사고 주입 확장)로 HTTP 경계를 시험한다.

느린 서버, 죽은 서버, 틀린 출력, 503 OUTPUT_INVALID(D75), 릴리스를 잊은 서버를 서비스가 어떻게 다루는지 본다."""
import threading
import time

import pytest
from boundary_contracts import BoundaryError

from survey_service.wiring.orchestrator import Orchestrator
from survey_service.components.release_loader import ReleaseLoader
from survey_service.components.release_registry import ReleaseRegistry
from survey_service.components.link_inference import RemoteInference, Stats
from survey_service.components.session_store import MemorySessionStore

from .timing import FakeClock, HANG_GUARD_SECONDS, wait_until
from .fakes import FakeReleaseReader, RecordingConsole, RecordingDispatcher
from .runtime_support import FlakyRuntime, raw_response, redirect_extraction
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED

TOKEN = "inf-tok"


class Env:
    def __init__(self, tmp, *, timeout=HANG_GUARD_SECONDS, retries=1, load_timeout=HANG_GUARD_SECONDS, clock=None, **srv_kw):
        self.reader = FakeReleaseReader(tmp / "gate")
        self.reader.add_release("rel-1", sample_bank(), action_rules_version=INSTALLED["action_rules_version"],
                                model_runtime_version=INSTALLED["model_runtime_version"])
        self.reader.set_pointer("rel-1")
        self.srv = FlakyRuntime({TOKEN: "survey-service"}, **srv_kw).start()
        self.url = self.srv.url
        self.registry = ReleaseRegistry()
        self.client = RemoteInference(self.url, TOKEN, timeout, load_timeout=load_timeout, retries=retries, poll_interval=0.05, backoff=0.01,
                                     **({"monotonic": clock, "sleep": clock.sleep} if clock else {}),
                                     extract_timeout=timeout, respond_timeout=timeout)   # 호출 상한은 이 시험들의 주제가 아니다: 가짜 서버가 즉시 답하므로 시한이 결과를 바꾸지 않는다(매달림 감시 값). 시한 처리는 아래 '시간 초과' 시험이 즉시 던지는 오류로 본다
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
    # 호출 상한 5초: 시간을 보지 않는 시험이 부하로 0.5초를 넘겨 LoRA-A 타임아웃으로 흔들렸다(왜 5초: 바깥 한도일 뿐 판정은 상태가 한다)
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


def _timeout_error():
    # 계약 클라이언트가 읽기 시한을 넘기면 내는 오류의 모양(reason에 Timeout). 시간 초과 자체는 클라이언트·라이브러리의 몫이라 즉시 던진다
    return BoundaryError("UNAVAILABLE", "추론 호출이 시간 안에 끝나지 않았다", {"reason": "TimeoutError"})


def test_extract_timeout_fails_the_step_without_retry(env):
    """extract가 시간 초과로 실패하면 그 단계를 실패시키고 다시 시도하지 않는다. 시한 값(3.0초)은 test_inference_time_budget.py가 설정에서 확인한다."""
    env.start()

    def boom(*a, **k):
        raise _timeout_error()
    env.registry.active().session._ops["extract"].extract = boom
    o = env.turn()
    cs = [c for c in env.client.stats.calls() if c["op"] == "extract"]
    assert len(cs) == 1 and cs[0]["timed_out"] and not cs[0]["ok"], cs
    assert failed(o) == LORA_A_FAIL
    assert o.orchestrator_output["turn_failure_signal"] == {"session_id": "S1", "write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}
    s = env.client.stats.snapshot()
    assert s["timeouts"] == 1 and s["retries"] == 0 and s["failures"] == 1


def test_respond_timeout_fails_after_commit_state_is_kept(env):
    env.start()

    def boom(*a, **k):
        raise _timeout_error()
    env.registry.active().session._ops["respond"].respond = boom      # respond가 시간 초과(즉시 던진다)
    from survey_service.components.session_store import answers_by_key
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
    n0 = len(env.client.stats.calls())
    o = env.turn()
    cs = env.client.stats.calls()[n0:]
    assert failed(o) == LORA_A_FAIL and len(cs) == 2 and all(c["elapsed"] < 1.0 for c in cs), cs      # 연결 거부는 매달리지 않고 바로 실패한다: 호출마다 경과가 짧다
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
    gate = threading.Event()
    gate.set()                                                         # 처음 적재는 바로 끝난다
    e = Env(tmp_path, load_gate=gate)
    try:
        assert e.loader.poll_once().action == "LOADED"                 # 적재 신호 뒤 끝날 때까지 기다린다
        e.start()
        gate.clear()                                                   # 서버가 릴리스를 잊고 다시 적재하는 동안 LOADING이다(시간과 무관)
        e.srv.forget()
        o = e.turn()
        assert failed(o) == LORA_A_FAIL and e.client.stats.snapshot()["reloads"] >= 1      # 적재를 기다리지 않고 이 턴을 실패시켰다(기다렸다면 게이트가 켜지기 전에는 끝나지 않는다)
        gate.set()                                                     # 서버가 다 올리면 다음 턴은 성공
        assert failed(e.turn()) == []
    finally:
        e.close()


def test_async_load_is_polled_until_loaded(tmp_path):
    e = Env(tmp_path, load_polls=3)                                    # 적재 상태를 세 번 물은 뒤에야 LOADED
    try:
        assert e.loader.poll_once().action == "LOADED"                 # 끝까지 폴링했다(시간 하한은 단언하지 않는다)
        assert e.registry.active_id() == "rel-1"
    finally:
        e.close()


def test_load_wait_timeout_is_transient_not_a_rejection(tmp_path):
    e = Env(tmp_path, load_gate=threading.Event(), load_timeout=1.0, clock=FakeClock())      # 게이트가 꺼져 있어 항상 LOADING. 가짜 시계가 폴링 대기를 흘려 실제로 기다리지 않고 한도를 넘긴다
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
    """느린 추론 하나가 다른 세션의 호출을 막지 않는다. 시간을 재지 않는다: 서버가 응답을 Event로 붙잡고 있는 동안 네 세션의 호출이 모두 서버에 도착했는지 본다(직렬이면 하나만 도착한다)."""
    e = Env(tmp_path)
    gate, lock, arrived = threading.Event(), threading.Lock(), []
    try:
        e.loader.poll_once()

        def respond(req):
            with lock:
                arrived.append(1)
            gate.wait(HANG_GUARD_SECONDS)       # 매달림 감시: 아래에서 네 호출이 모두 도착한 뒤 푼다
            return ("응답입니다.", "ok")
        e.srv.respond_fn = respond
        ts = [threading.Thread(target=lambda i=i: e.orch.handle_turn(turn_id="a", frontend_message={"session_id": f"P{i}", "kind": "SESSION_START"}))
              for i in range(4)]
        [t.start() for t in ts]
        assert wait_until(lambda: len(arrived) >= 4)      # 매달림 감시
        gate.set()
        [t.join(HANG_GUARD_SECONDS) for t in ts]
    finally:
        gate.set(); e.close()


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
    s = env.registry.active().session
    for rt in (s._rt, *s._ops.values()):      # 제어용과 호출 종류별 클라이언트 모두
        rt._token = "wrong"
    o = env.turn()
    assert failed(o) == LORA_A_FAIL and env.client.stats.snapshot()["retries"] == 0


def test_protections_are_summed_per_rule():
    s = Stats()
    s.add_protections({"G0": 2, "G5": 1})
    s.add_protections({"G0": 1})
    assert s.snapshot()["protections"] == {"G0": 3, "G5": 1}
