"""FAULT_POLICY 2·3: 같은 turn_id의 동시 중복(처리 중이면 409 + Retry-After, 확정 뒤에는 저장된 결과), 서버 턴 총 마감."""
import threading

from survey_service.components.link_public_api import PublicApi

from .timing import HANG_GUARD_SECONDS, FakeClock, wait_until
from .e2e_support import ANSWER_OUT, Rig


def _api(rig):
    return PublicApi(rig.orch, "tok", max_utterance_chars=100)


def _call(api, tid, text="안 피움", sid="K1"):
    import json
    body = {"session_id": sid, "kind": "UTTERANCE", "utterance": text, "turn_id": tid}
    return api.handle("POST", f"/v1/sessions/{sid}/turns", "Bearer tok", json.dumps(body, ensure_ascii=False).encode())


def _start(api):
    import json
    return api.handle("POST", "/v1/sessions", "Bearer tok", json.dumps({"session_id": "K1", "kind": "SESSION_START"}).encode())


def _gate_extract(rig):
    """extract가 gate가 열릴 때까지 멈추게 한다. entered는 첫 호출이 들어왔다는 신호."""
    gate, entered = threading.Event(), threading.Event()
    real = rig.session.extract

    def slow(req, **kw):
        entered.set()
        gate.wait(HANG_GUARD_SECONDS)
        return real(req, **kw)
    rig.session.extract = slow
    return gate, entered


def test_concurrent_same_turn_id_is_409_while_in_progress_then_replays_once():
    rig = Rig(risk=True)
    api = _api(rig)
    assert _start(api)[0] == 200
    rig.model.queue.append(ANSWER_OUT)
    gate, entered = _gate_extract(rig)
    first: list = []
    th = threading.Thread(target=lambda: first.append(_call(api, "t-1")))
    th.start()
    assert entered.wait(HANG_GUARD_SECONDS)
    code, body, hdr = _call(api, "t-1")                       # 처리 중인 같은 turn_id
    assert code == 409 and body["code"] == "CONFLICT" and body["details"] == {"reason": "TURN_IN_PROGRESS"} and body["retryable"] is False
    assert "Retry-After" in hdr and "TURN_IN_PROGRESS" not in body["message"]
    gate.set()
    th.join(HANG_GUARD_SECONDS)
    assert first[0][0] == 200
    n_log = len(rig.doc("K1")["dialogue_log"])
    rig.model.queue.append(ANSWER_OUT)                        # 두 번 처리됐다면 소비됐을 것
    again = _call(api, "t-1")
    assert again[:2] == first[0][:2] and len(rig.model.queue) == 1
    assert len(rig.doc("K1")["dialogue_log"]) == n_log
    assert rig.orch.counters.get("turn_in_progress") == 1


def test_many_concurrent_same_turn_id_process_exactly_once():
    rig = Rig(risk=True)
    api = _api(rig)
    _start(api)
    rig.model.queue.extend([ANSWER_OUT] * 5)
    gate, entered = _gate_extract(rig)
    res: list = []
    ths = [threading.Thread(target=lambda: res.append(_call(api, "t-9"))) for _ in range(6)]
    for t in ths:
        t.start()
    assert entered.wait(HANG_GUARD_SECONDS)
    assert wait_until(lambda: len(res) >= 5)      # 첫 호출만 처리 중이고 나머지 다섯은 409로 바로 돌아온다(고정 sleep으로 추측하지 않는다)
    gate.set()
    for t in ths:
        t.join(HANG_GUARD_SECONDS)
    codes = sorted(r[0] for r in res)
    assert codes.count(200) >= 1 and set(codes) <= {200, 409}


def say_past_deadline(rig, clock, entered, tid, text="안 피움"):
    """턴을 다른 스레드에서 시작하고, 일이 막힌 뒤(entered) 주입한 시계로 턴 마감을 넘긴다. 실제로 기다리지 않는다. 돌려주는 값은 호출자가 받은 결과다."""
    res = []
    th = threading.Thread(target=lambda: res.append(_say(rig, tid, text)), daemon=True)
    th.start()
    assert entered.wait(HANG_GUARD_SECONDS)
    clock.advance(rig.orch.turn_deadline_seconds + 1)
    th.join(HANG_GUARD_SECONDS)
    assert not th.is_alive()
    return res[0]


def join_turn_worker(tid):
    """마감으로 버려진 턴의 작업 스레드(turn-<id>)가 끝나기를 기다린다: 끝난 뒤에 확정하지 않았는지 본다(고정 sleep으로 추측하지 않는다)."""
    for th in threading.enumerate():
        if th.name == f"turn-{tid}":
            th.join(HANG_GUARD_SECONDS)


def _say(rig, tid, text="안 피움"):
    return rig.orch.handle_turn(turn_id=tid, frontend_message={"session_id": "S1", "kind": "UTTERANCE", "utterance": text})


def _answers(rig):
    return rig.doc("S1")["answer_records"][0]["current_value"]


def test_deadline_slow_model_ends_not_committed_and_late_work_does_not_commit():
    clock = FakeClock()
    rig = Rig(turn_deadline_seconds=9.0, clock=clock)
    rig.start()
    before = _answers(rig)
    rig.model.queue.append(ANSWER_OUT)
    gate, entered = _gate_extract(rig)
    out = say_past_deadline(rig, clock, entered, "t-slow")      # 모델이 마감 전에 끝나지 않는다(gate를 풀지 않음)
    sig = out.frontend_input["turn_failure_signal"]
    assert sig["write_committed"] == "NOT_COMMITTED" and out.frontend_input["response_text"] is None
    assert rig.orch.counters.get("turn_deadline_exceeded") == 1
    commits = []
    real = rig.store.commit
    rig.store.commit = lambda *a, **k: (commits.append(1), real(*a, **k))[1]
    gate.set()                                        # 늦게 끝난 모델 작업
    join_turn_worker("t-slow")                        # 그 작업이 끝난 뒤에 본다
    assert not commits and _answers(rig) == before    # 확정하지 않았다
    assert rig.store.get_turn("S1", "t-slow") is None
    rig.model.queue.append(ANSWER_OUT)                # 같은 turn_id 재전송은 안전하게 새로 처리된다
    rig.session.extract = lambda req, real=rig.session.__class__.extract, s=rig.session, **kw: real(s, req, **kw)
    assert _say(rig, "t-slow").frontend_input["turn_failure_signal"] is None and _answers(rig) != before


def test_deadline_slow_store_load_skips_commit():
    clock = FakeClock()
    rig = Rig(turn_deadline_seconds=9.0, clock=clock)
    rig.start()
    before = _answers(rig)
    real_load = rig.store.load
    slow = threading.Event()
    release, entered = threading.Event(), threading.Event()

    def load(sid):
        if slow.is_set():
            entered.set()
            release.wait(HANG_GUARD_SECONDS)          # DB 대기: 턴이 마감으로 돌아온 뒤에 풀어 준다
        return real_load(sid)
    rig.store.load = load
    rig.model.queue.append(ANSWER_OUT)
    commits = []
    real = rig.store.commit
    rig.store.commit = lambda *a, **k: (commits.append(1), real(*a, **k))[1]
    slow.set()
    out = say_past_deadline(rig, clock, entered, "t-db")
    assert out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    release.set()                                     # 늦게 끝난 읽기: 이 뒤에 확정하지 않아야 한다
    join_turn_worker("t-db")
    assert not commits and _answers(rig) == before


def test_deadline_not_exceeded_is_normal():
    rig = Rig(turn_deadline_seconds=5)
    rig.start()
    out = rig.say("안 피움", out=ANSWER_OUT)
    assert out.frontend_input["turn_failure_signal"] is None and rig.orch.counters.get("turn_deadline_exceeded") is None


def test_input_mismatch_409_is_distinguishable_from_in_progress():
    rig = Rig(risk=True)
    api = _api(rig)
    _start(api)
    rig.model.queue.append(ANSWER_OUT)
    assert _call(api, "t-m", "안 피움")[0] == 200
    code, body, hdr = _call(api, "t-m", "다른 말")            # 같은 turn_id, 다른 입력
    assert code == 409 and body["code"] == "CONFLICT" and "details" not in body and "Retry-After" not in hdr
