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
    assert code == 409 and body["message"].startswith("TURN_IN_PROGRESS") and "Retry-After" in hdr
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
