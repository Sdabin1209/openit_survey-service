"""단계 B (3): 한 턴의 모델 호출 두 번이 턴 총 마감 9초 안에 들어온다. 호출별 상한 extract 3.0초, respond 5.0초,
재시도는 남은 턴 예산이 그 호출 상한 이상일 때만 한다.

기대값의 출처: 사용자 결정(2026-10-30)과 계약 CONVENTIONS의 시간 상한 사슬(ADR-0049): extract 3.0 + respond 5.0 = 8.0, 서버 처리 여유 1.0을 더해 턴 마감 9.0 < gateway 10.5 < 키오스크 12.
"재시도는 남은 턴 예산이 호출 상한 이상일 때만 한다. 남은 예산이 모자라면 재시도하지 않고 실패로 처리한다". 값은 측정이 아니라 잠정값이다(GPU 실측 전).
'끊긴 시점'은 턴 전체 벽시계가 아니라 추론 클라이언트가 기록한 호출 경과(Stats.calls)로 잰다: 턴 전체에는 끊긴 뒤의 처리와 부하에 따른 스레드 지연이 들어가 좁은 상한이
흔들렸다(check-all 동시 실행에서 2.42초). 호출 경과는 소켓 읽기 타임아웃이 끊은 시점이다. 호출이 타임아웃으로 실패했다는 사실이 '서버 응답보다 먼저 끊었다'는 증거라 위쪽 상한은 따로 두지 않는다.
시험은 느린 가짜 추론 서버(tests/runtime_support.FlakyRuntime)로 실제 시간을 잰다(경과는 time.monotonic: WSL2 벽시계는 수십~백여 ms 되감긴 적이 있다).
"""
import time

import pytest
from boundary_contracts import BoundaryError

from survey_service.wiring.orchestrator import Orchestrator
from survey_service.components.link_inference import RemoteInference
from survey_service.settings import Settings

from .fakes import RecordingConsole, RecordingDispatcher
from .test_inference_client import LORA_A_FAIL, TOKEN, Env, failed
from .runtime_support import redirect_extraction


class BudgetEnv(Env):
    """Env와 같으나 추론 클라이언트는 서비스 기본(이전 설정과 같은 제어 호출 timeout 4초 하나만 줌), 턴 마감은 인자로."""
    def __init__(self, tmp, turn_deadline=9.0):
        super().__init__(tmp, timeout=4.0)
        self.client = RemoteInference(self.url, TOKEN, 4.0, load_timeout=5.0, retries=1, poll_interval=0.05, backoff=0.01)
        self.loader.inference = self.client
        self.orch = Orchestrator(self.orch.store, self.registry, staff_console=RecordingConsole(), dispatcher=RecordingDispatcher(),
                                 turn_deadline_seconds=turn_deadline)

    def slow(self, extract=0.0, respond=0.0):
        def ex(req):
            time.sleep(extract)
            return redirect_extraction(req)

        def rs(req):
            time.sleep(respond)
            return ("응답입니다.", "ok")
        self.srv.extract_fn, self.srv.respond_fn = ex, rs


def _last(env, op):
    return [c for c in env.client.stats.calls() if c["op"] == op][-1]


@pytest.fixture
def be(tmp_path):
    e = BudgetEnv(tmp_path)
    assert e.loader.poll_once().action == "LOADED"
    e.start()
    yield e
    e.close()


def test_default_settings_carry_the_contract_budget():
    s = Settings()
    assert s.inference_extract_timeout_seconds == 3.0 and s.inference_respond_timeout_seconds == 5.0
    assert s.turn_deadline_seconds == 9.0
    assert s.inference_extract_timeout_seconds + s.inference_respond_timeout_seconds + 1.0 == s.turn_deadline_seconds      # 합 8.0 + 여유 1.0 = 9.0


def test_extract_is_cut_at_3_seconds_not_later(be):
    be.slow(extract=3.5)
    o = be.turn()
    c = _last(be, "extract")
    # 3.5초 걸리는 extract는 3.0초에서 끊긴다(이전 4초면 3.5초에 성공한다): 호출이 타임아웃으로 실패했고 경과가 상한(3.0) 근처 이상이다.
    # 끊겼다는 사실 자체가 3.5초 전이라는 증거라 위쪽 상한은 따로 두지 않는다(부하에 따른 지연을 단언하지 않는다).
    assert not c["ok"] and c["timed_out"] and c["elapsed"] >= 2.9, c
    assert failed(o) == LORA_A_FAIL


def test_respond_is_cut_at_2_seconds_not_later(be):
    be.slow(respond=5.5)
    be.turn()
    c = _last(be, "respond")
    assert not c["ok"] and c["timed_out"] and c["elapsed"] >= 4.9, c          # 5.5초 걸리는 respond는 5.0초에서 끊긴다(끊겼다는 사실이 5.5초 전이라는 증거)


def test_both_calls_at_their_limits_still_fit_the_turn_deadline(be):
    be.slow(extract=2.8, respond=4.8)
    o = be.turn()
    cs = be.client.stats.calls()[-2:]
    # 턴 마감 9초는 Orchestrator가 지킨다: 마감을 넘기면 실패 신호가 나온다. 그래서 '신호 없음'이 마감 안에 끝났다는 증거다.
    # 호출 경과의 합(2.8 + 4.8 = 7.6초 + 처리)이 9초 안인지도 본다. 벽시계 총합은 부하에 따라 흔들려 단언하지 않는다.
    assert o.orchestrator_output["turn_failure_signal"] is None and all(c["ok"] for c in cs), cs
    assert sum(c["elapsed"] for c in cs) < 9.0, cs


def test_worst_case_with_both_calls_timing_out_is_within_9_seconds(be):
    be.slow(extract=0.1, respond=12.0)            # extract 성공 뒤 respond가 매달린다: 0.1 + 5.0
    be.turn()
    cs = be.client.stats.calls()[-2:]
    assert [c["op"] for c in cs] == ["extract", "respond"] and cs[1]["timed_out"], cs      # respond는 한 번 끊기고 재시도하지 않는다
    assert sum(c["elapsed"] for c in cs) < 9.0, cs        # 호출 경과의 합이 턴 마감 9초 안(벽시계 총합은 부하에 흔들려 재지 않는다)


def test_no_retry_when_remaining_turn_budget_is_below_the_call_limit(tmp_path):
    e = BudgetEnv(tmp_path, turn_deadline=2.0)    # 남은 예산 약 2초 < extract 상한 3초
    try:
        assert e.loader.poll_once().action == "LOADED"
        e.start()
        e.srv.fail_next = [BoundaryError("UNAVAILABLE", "일시 장애")]
        before = list(e.srv.calls)
        e.turn()
        assert e.client.stats.snapshot()["retries"] == 0
        assert e.srv.calls[len(before):].count("extract") <= 1
    finally:
        e.close()


def test_retry_happens_when_remaining_turn_budget_covers_the_call_limit(be):
    be.srv.fail_next = [BoundaryError("UNAVAILABLE", "일시 장애")]
    before = len(be.srv.calls)
    o = be.turn()
    assert be.client.stats.snapshot()["retries"] == 1                    # 남은 예산 약 9초 >= 3초: 재시도한다
    assert be.srv.calls[before:].count("extract") == 2 and o.orchestrator_output["turn_failure_signal"] is None
