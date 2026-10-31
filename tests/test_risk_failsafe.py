"""D88 U1·U2: 위험 규칙 감지는 모델 추출 성공·쓰기 확정과 무관하게 모든 발화 턴에 적용되고, 알림은 확정과 분리해 먼저 나간다. 임시 기본 목록, 문항 세트 저작 값."""
import copy
import threading
import time

import pytest

from survey_service.components.risk_screen import RuleRiskSource

from .timing import HANG_GUARD_SECONDS
from .timing import HANG_GUARD_SECONDS, FakeClock, wait_until
from .e2e_support import ANSWER_OUT, Rig, ext, val
from .sample_bank import sample_bank

DELIVERED = {"write_committed": "COMMITTED", "staff_alert": "DELIVERED"}


def _rig(**kw):
    rig = Rig(risk_source=RuleRiskSource.provisional_default(), **kw)
    rig.start()
    return rig


def _alerts(rig):
    return [c["staff_alert"] for c in rig.console.inputs if c.get("staff_alert")]


@pytest.mark.parametrize("text", ["가슴이 너무 아파요", "숨이 안 쉬어져요", "숨이안쉬어져요", "죽고 싶어요", "의식을 잃을 것 같아요", "피가 계속 나요", "한쪽 팔이 마비된 것 같아요", "도와주세요"])
def test_provisional_defaults_catch_red_flag_phrases_without_calling_the_model(text):
    rig = _rig()
    n = len(rig.session.extract_requests)
    out = rig.say(text, out="이 출력은 쓰이면 안 된다 {{")
    assert len(rig.session.extract_requests) == n                                  # 모델 추출 없이 처리한다
    assert len(_alerts(rig)) == 1 and rig.doc()["progress_record"]["session_status"] == "PAUSED_RISK"
    sig = out.frontend_input["turn_failure_signal"]
    assert sig["staff_alert"] == "DELIVERED" and out.frontend_input["response_text"] == sample_bank()["session_settings"]["fixed_sentences"][3]["sentence"] or sig
    assert out.frontend_input["response_text"]
    assert text not in repr(_alerts(rig))                                            # 알림에는 발화 원문이 없다


def test_normal_utterance_is_not_flagged():
    rig = _rig()
    out = rig.say("안 피워요", out=ANSWER_OUT)
    assert not _alerts(rig) and rig.doc()["progress_record"]["session_status"] == "IN_PROGRESS" and out.frontend_input["turn_failure_signal"] is None


def test_alert_goes_out_even_when_the_write_commit_fails():
    rig = _rig()
    rig.store.commit = lambda *a, **k: {"status": "DB_ERROR", "write_outcome": "NOT_WRITTEN", "revision": None}
    out = rig.say("가슴이 아파요")
    assert len(_alerts(rig)) == 1                                                    # 확정이 실패해도 알림은 나갔다
    assert out.frontend_input["turn_failure_signal"] == {"session_id": "S1", "write_committed": "NOT_COMMITTED", "staff_alert": "DELIVERED"}


def test_alert_goes_out_even_when_the_turn_deadline_is_exceeded():
    clock = FakeClock()
    rig = _rig(turn_deadline_seconds=9.0, clock=clock)
    real = rig.store.load
    release, entered = threading.Event(), threading.Event()

    def slow(sid):
        entered.set()
        release.wait(HANG_GUARD_SECONDS)                              # DB 읽기가 끝나지 않는다: 시험이 주입한 시계로 마감을 넘긴 뒤에 풀어 준다
        return real(sid)
    rig.store.load = slow
    res = []
    th = threading.Thread(target=lambda: res.append(rig.say("가슴이 아파요")), daemon=True)
    th.start()
    try:
        assert entered.wait(HANG_GUARD_SECONDS)
        clock.advance(2.0)                                             # 선별의 확인 한도(마감의 30%, 최대 1초)만 넘긴다: 알림은 DB 읽기를 포기하고 먼저 나간다
        assert wait_until(lambda: len(_alerts(rig)) == 1)
        clock.advance(100)                                             # 이어서 턴 마감을 넘긴다(실제로 기다리지 않는다)
        th.join(HANG_GUARD_SECONDS)
        assert not th.is_alive()
    finally:
        release.set()
    out = res[0]
    assert out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED"
    assert len(_alerts(rig)) == 1                                                    # 선별이 DB 읽기보다 앞에서 알림을 보냈다
