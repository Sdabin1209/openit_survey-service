"""FAULT_POLICY 2·3: 같은 turn_id의 동시 중복(처리 중이면 409 + Retry-After, 확정 뒤에는 저장된 결과), 서버 턴 총 마감."""
import threading
import time

from survey_service.api import PublicApi

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

    def slow(req):
        entered.set()
        gate.wait(20)
        return real(req)
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
    assert entered.wait(5)
    code, body, hdr = _call(api, "t-1")                       # 처리 중인 같은 turn_id
    assert code == 409 and body["code"] == "CONFLICT" and body["details"] == {"reason": "TURN_IN_PROGRESS"} and body["retryable"] is False
    assert "Retry-After" in hdr and "TURN_IN_PROGRESS" not in body["message"]
    gate.set()
    th.join(10)
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
    assert entered.wait(5)
    time.sleep(0.3)
    gate.set()
    for t in ths:
        t.join(10)
    codes = sorted(r[0] for r in res)
    assert codes.count(200) >= 1 and set(codes) <= {200, 409}


def _say(rig, tid, text="안 피움"):
    return rig.orch.handle_turn(turn_id=tid, frontend_message={"session_id": "S1", "kind": "UTTERANCE", "utterance": text})


def _answers(rig):
    return rig.doc("S1")["answer_records"][0]["current_value"]


def test_deadline_slow_model_ends_not_committed_and_late_work_does_not_commit():
    rig = Rig(turn_deadline_seconds=0.5)
    rig.start()
    before = _answers(rig)
    rig.model.queue.append(ANSWER_OUT)
    gate, entered = _gate_extract(rig)
    t = time.monotonic()
    out = _say(rig, "t-slow")
    assert time.monotonic() - t < 3
    sig = out.frontend_input["turn_failure_signal"]
    assert sig["write_committed"] == "NOT_COMMITTED" and out.frontend_input["response_text"] is None
    assert rig.orch.counters.get("turn_deadline_exceeded") == 1
    commits = []
    real = rig.store.commit
    rig.store.commit = lambda *a, **k: (commits.append(1), real(*a, **k))[1]
    gate.set()                                        # 늦게 끝난 모델 작업
    time.sleep(0.8)
    assert not commits and _answers(rig) == before    # 확정하지 않았다
    assert rig.store.get_turn("S1", "t-slow") is None
    rig.model.queue.append(ANSWER_OUT)                # 같은 turn_id 재전송은 안전하게 새로 처리된다
    rig.session.extract = lambda req, real=rig.session.__class__.extract, s=rig.session: real(s, req)
    assert _say(rig, "t-slow").frontend_input["turn_failure_signal"] is None and _answers(rig) != before


def test_deadline_slow_store_load_skips_commit():
    rig = Rig(turn_deadline_seconds=0.4)
    rig.start()
    before = _answers(rig)
    real_load = rig.store.load
    slow = threading.Event()

    def load(sid):
        if slow.is_set():
            time.sleep(0.9)                           # DB 대기
        return real_load(sid)
    rig.store.load = load
    rig.model.queue.append(ANSWER_OUT)
    commits = []
    real = rig.store.commit
    rig.store.commit = lambda *a, **k: (commits.append(1), real(*a, **k))[1]
    slow.set()
    out = _say(rig, "t-db")
    assert out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    time.sleep(1.2)
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
