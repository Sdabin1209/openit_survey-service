"""추론 서버 호출의 실패·지연·잘못된 출력 처리(docs/inference-server-spec.md 5절). 상대는 참조 추론 서버(느리게, 죽게, 틀리게 만든다)."""
import json
import threading
import time

import pytest

import model_runtime
from survey_service.inference import InferenceServer
from survey_service.orchestrator import Orchestrator
from survey_service.release import ReleaseLoader, ReleaseRegistry
from survey_service.remote import RemoteInference
from survey_service.settings import Settings
from survey_service.store.memory import MemorySessionStore

from .fakes import FakeReleaseReader, RecordingConsole, RecordingDispatcher
from .ref_inference_server import build, err, make_server
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED

TOKEN = "inf-tok"


class Env:
    def __init__(self, tmp, *, timeout=0.5, retries=1, load_timeout=5.0, inference=None, **srv_kw):
        self.reader = FakeReleaseReader(tmp / "gate")
        self.reader.add_release("rel-1", sample_bank(), turn_engine_version=INSTALLED["turn_engine_version"],
                                model_runtime_version=INSTALLED["model_runtime_version"])
        self.reader.set_pointer("rel-1")
        s = Settings(work_dir=str(tmp / "inf-work"), release_reader="x:y", trace_ingest="x:y", model_backend="fake")
        self.inf = build(s, reader=self.reader, token=TOKEN, inference=inference, **srv_kw)
        self.httpd = make_server(self.inf, 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.registry = ReleaseRegistry()
        self.client = RemoteInference(self.url, TOKEN, timeout, load_timeout=load_timeout, retries=retries, poll_interval=0.05, backoff=0.01)
        self.loader = ReleaseLoader(self.reader, self.registry, self.client, workdir=tmp / "svc-work", installed_versions=INSTALLED)
        self.orch = Orchestrator(MemorySessionStore(), self.registry, staff_console=RecordingConsole(), dispatcher=RecordingDispatcher())
        self.n = 0

    def turn(self, text="안녕하세요", sid="S1"):
        self.n += 1
        return self.orch.handle_turn(turn_id=f"t{self.n}", frontend_message={"session_id": sid, "kind": "UTTERANCE", "utterance": text})

    def start(self, sid="S1"):
        self.n += 1
        return self.orch.handle_turn(turn_id=f"t{self.n}", frontend_message={"session_id": sid, "kind": "SESSION_START"})

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    assert e.loader.poll_once().action == "LOADED"
    yield e
    e.close()


def failed(o):
    tr = o.orchestrator_output["log_entry"]["turn_result"]
    return tr["failed_steps"] if isinstance(tr, dict) else []


def test_normal_turn_and_stats(env):
    env.start()
    o = env.turn()
    assert o.orchestrator_output["turn_failure_signal"] is None
    s = env.client.stats.snapshot()
    assert s["calls"] == 3 and s["failures"] == 0 and s["retries"] == 0


def test_slow_but_within_timeout_is_fine(env):
    env.inf.delay = 0.2
    o = env.start()
    assert o.frontend_input["response_text"] and o.orchestrator_output["turn_failure_signal"] is None


def test_slow_parse_beyond_timeout_fails_the_step_without_retry(env):
    env.start()
    env.inf.delay = 2.0
    t0 = time.time()
    o = env.turn()
    el = time.time() - t0
    assert 0.4 < el < 1.5                                            # 타임아웃(0.5s)에서 끊고 다시 시도하지 않는다
    assert failed(o) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]
    assert o.orchestrator_output["turn_failure_signal"] == {"session_id": "S1", "write_committed": "NOT_COMMITTED", "staff_alert": "NONE"}
    s = env.client.stats.snapshot()
    assert s["timeouts"] == 1 and s["retries"] == 0 and s["failures"] == 1
    assert [c for c in env.inf.calls if c[0] == "parse"].__len__() == 1


def test_slow_generate_fails_after_commit_state_is_kept(env):
    env.start()
    orig = env.registry.active().session.generate

    def slow_generate(inp):
        env.inf.delay = 2.0
        return orig(inp)
    env.registry.active().session.generate = slow_generate
    o = env.turn()
    assert failed(o) == [{"phase": "AFTER_COMMIT", "step": "LoRA-B"}]
    assert o.orchestrator_output["turn_failure_signal"]["write_committed"] == "COMMITTED" and o.frontend_input["response_text"] is None
    assert env.orch.store.load("S1")["state"]["planner_record"]["ask_counts"] is not None


def test_one_503_is_retried_and_succeeds(env):
    env.start()
    env.inf.fail_next = [err("UNAVAILABLE", "과부하")]
    o = env.turn()
    assert failed(o) == [] and env.client.stats.snapshot()["retries"] == 1
    assert [c[0] for c in env.inf.calls][-3:] == ["parse", "parse", "generate"]


def test_persistent_503_fails_after_retries(env):
    env.start()
    env.inf.fail_next = [err("UNAVAILABLE", "x")] * 2
    o = env.turn()
    assert failed(o) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]
    s = env.client.stats.snapshot()
    assert s["retries"] == 1 and s["failures"] == 1


def test_retries_zero_means_single_attempt(tmp_path):
    e = Env(tmp_path, retries=0)
    try:
        e.loader.poll_once(); e.start()
        e.inf.fail_next = [err("UNAVAILABLE", "x")]
        assert failed(e.turn()) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}] and e.client.stats.snapshot()["retries"] == 0
    finally:
        e.close()


def test_server_down_fails_fast_after_one_retry(env):
    env.start()
    env.close()
    t0 = time.time()
    o = env.turn()
    assert time.time() - t0 < 1.5 and failed(o) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]
    assert env.client.stats.snapshot()["retries"] == 1


def test_400_from_server_is_not_retried(env):
    env.start()
    env.inf.fail_next = [err("INVALID_RECORD", "입력이 계약을 어긴다")]
    o = env.turn()
    assert failed(o) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}] and env.client.stats.snapshot()["retries"] == 0


@pytest.mark.parametrize("bad", [
    {"items": "not a list"}, {"items": [{"item_no": 0}]}, {"nothing": 1}, [], {"items": [], "extra": 1},
])
def test_invalid_parser_output_fails_the_step(env, bad):
    env.start()
    env.inf.fail_next = [(200, bad, {})]
    o = env.turn()
    assert failed(o) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]
    s = env.client.stats.snapshot()
    assert s["invalid_outputs"] == 1 and s["retries"] == 0


@pytest.mark.parametrize("bad", [{"response_text": ""}, {"response_text": 3, "is_fallback": False}, {"is_fallback": False}, {"response_text": "x"}])
def test_invalid_generator_output_fails_after_commit(env, bad):
    env.start()
    calls = {"n": 0}
    real = env.inf.handle

    def handle(method, path, auth, body):
        if path.endswith("/generate"):
            return 200, bad, {}
        return real(method, path, auth, body)
    env.inf.handle = handle
    o = env.turn()
    assert failed(o) == [{"phase": "AFTER_COMMIT", "step": "LoRA-B"}] and o.frontend_input["response_text"] is None
    assert env.client.stats.snapshot()["invalid_outputs"] == 1


def test_non_json_body_is_a_failure(env):
    env.start()
    env.inf.fail_next = [(200, b"<html>gateway</html>", {})]
    assert failed(env.turn()) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]


def test_forgotten_release_is_reloaded_once(env):
    env.start()
    env.inf.registry._loaded.clear(); env.inf._state.clear()          # 추론 컨테이너가 다시 떴다
    o = env.turn()
    assert failed(o) == [] and env.client.stats.snapshot()["reloads"] == 1


def test_forgotten_release_with_slow_reload_fails_the_turn_instead_of_waiting(tmp_path):
    e = Env(tmp_path, async_load=True, load_delay=1.0)
    try:
        assert e.loader.poll_once().action == "LOADED"                 # 적재 신호 뒤 끝날 때까지 기다린다
        e.start()
        e.inf.registry._loaded.clear(); e.inf._state.clear()
        t0 = time.time()
        o = e.turn()
        assert time.time() - t0 < 0.9 and failed(o) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}]
        time.sleep(1.3)                                                # 서버가 다 올리면 다음 턴은 성공
        assert failed(e.turn()) == []
    finally:
        e.close()


def test_async_load_is_polled_until_loaded(tmp_path):
    e = Env(tmp_path, async_load=True, load_delay=0.4)
    try:
        t0 = time.time()
        assert e.loader.poll_once().action == "LOADED" and time.time() - t0 >= 0.35
        assert e.registry.active_id() == "rel-1"
    finally:
        e.close()


def test_load_timeout_rejects_release_and_keeps_previous(tmp_path):
    e = Env(tmp_path, async_load=True, load_delay=1.5, load_timeout=0.3)
    try:
        r = e.loader.poll_once()
        assert r.action == "REJECTED" and r.code == "REMOTE_TIMEOUT" and e.registry.active() is None
    finally:
        e.close()


def test_server_side_load_failure_is_a_rejection(tmp_path):
    e = Env(tmp_path, async_load=True)
    try:
        e.inf.loader.reader = FakeReleaseReader(tmp_path / "empty")      # 추론 서버가 읽는 I4에는 이 릴리스가 없다
        r = e.loader.poll_once()
        assert r.action == "REJECTED" and r.code == "REMOTE_FAILED_PRECONDITION" and e.registry.active() is None
    finally:
        e.close()


def test_other_sessions_are_not_blocked_by_slow_inference(tmp_path):
    e = Env(tmp_path, timeout=3.0)
    try:
        e.loader.poll_once()
        e.inf.delay = 0.3
        t0 = time.time()
        ts = [threading.Thread(target=lambda i=i: e.orch.handle_turn(turn_id="a", frontend_message={"session_id": f"P{i}", "kind": "SESSION_START"}))
              for i in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert time.time() - t0 < 0.9                                   # 직렬이면 4 x 0.3 = 1.2초 이상
    finally:
        e.close()


def test_server_side_output_protection_is_visible_and_turn_continues(tmp_path):
    """모델이 깨진 출력을 내면 서버의 출력 보호(D36)가 대체 출력을 만든다. 서비스는 턴을 실패시키지 않고 보호 건수만 센다."""
    def garbage(req):
        return "이건 JSON이 아니다" if req.role == "LORA_A" else json.dumps({"response_text": "응답입니다."})
    inf = InferenceServer("fake", backend_factory=lambda: model_runtime.FakeBackend(garbage))
    e = Env(tmp_path, inference=inf)
    try:
        e.loader.poll_once()
        e.start()
        o = e.turn()
        assert failed(o) == [] and o.trace["parser_output"] == {"items": []}
        assert e.client.stats.snapshot()["outcome_fallback"] >= 1
    finally:
        e.close()


def test_auth_failure_on_calls_fails_the_step(env):
    env.start()
    env.registry.active().session._http.token = "wrong"
    o = env.turn()
    assert failed(o) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}] and env.client.stats.snapshot()["retries"] == 0


def test_spec_endpoints_over_raw_http(env):
    """명세 3절의 응답 모양을 HTTP로 직접 확인한다(실제 서버가 따라야 할 모양)."""
    import urllib.request
    def raw(method, path, body=None, token=TOKEN):
        req = urllib.request.Request(env.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read()), r.headers
        except Exception as e:
            return e.code, json.loads(e.read()), e.headers
    assert raw("GET", "/healthz", token=None)[0] == 200 and raw("GET", "/readyz", token=None)[0] == 200
    assert raw("GET", "/v1/info")[1]["supported_contract_versions"] == [1]
    assert raw("GET", "/v1/releases/rel-1")[1]["state"] == "LOADED" and raw("GET", "/v1/releases/zzz")[1]["state"] == "ABSENT"
    assert raw("PUT", "/v1/releases/rel-1")[:2] == (200, {"release_id": "rel-1", "state": "LOADED", "error": None})
    code, body, h = raw("POST", "/v1/releases/rel-1/parse", {"utterance": "x", "last_directive": None, "awaiting_questions": [],
                                                          "question_names": [], "answered_slots": [], "guide_list": []})
    assert code == 200 and body == {"items": []} and h["X-Openit-Outcome"] == "ok" and int(h["X-Openit-Attempts"]) >= 1
    assert raw("POST", "/v1/releases/rel-1/parse", {"utterance": 1})[0] == 400
    assert raw("POST", "/v1/releases/nope/parse", {})[0] == 404
    code, body, _ = raw("PUT", "/v1/releases/rel-1", token="bad")
    assert code == 403 and body["code"] == "PERMISSION_DENIED" and body["retryable"] is False


def test_protection_counts_per_rule_are_exposed(tmp_path):
    """X-Openit-Protections의 규칙별 건수가 /readyz inference.protections로 나간다(모델이 깨진 출력을 내는 서버)."""
    def garbage(req):
        return "JSON 아님" if req.role == "LORA_A" else json.dumps({"response_text": "응답입니다."})
    e = Env(tmp_path, inference=InferenceServer("fake", backend_factory=lambda: model_runtime.FakeBackend(garbage)))
    try:
        e.loader.poll_once()
        e.start(); e.turn(); e.turn()
        snap = e.client.stats.snapshot()
        assert snap["outcome_fallback"] >= 2 and snap["protections"] and all(v >= 1 for v in snap["protections"].values())
        assert all(k[0] in "PG" for k in snap["protections"])            # 규칙 코드(P1~P6, G1·G4·G5)
    finally:
        e.close()


def test_protections_header_parsing_ignores_garbage():
    from survey_service.remote import Stats
    s = Stats()
    s.add_protections("P1:2, G5:1,bad,P1:x,:3")
    s.add_protections("P1:1")
    assert s.snapshot()["protections"] == {"P1": 3, "G5": 1}


def test_info_reports_serving_config_hash_per_role(env):
    code, body, _ = env.inf.handle("GET", "/v1/info", f"Bearer {TOKEN}", b"")
    assert code == 200 and set(body["serving_config_hash"]) == {"LORA_A", "LORA_B"}
