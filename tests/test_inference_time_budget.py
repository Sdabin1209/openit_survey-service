"""단계 B (3): 한 턴의 모델 호출 두 번이 턴 총 마감 6초 안에 들어온다. 호출별 상한 extract 3.0초, respond 2.0초,
재시도는 남은 턴 예산이 그 호출 상한 이상일 때만 한다.

기대값의 출처: ../boundary-contracts/CONVENTIONS.md 121~123행(2.2.0, D106): "model-runtime 요청 상한 extract 3.0초, respond 2.0초 ... 호출별 상한의 합(5.0초)에
서버 처리 여유 1.0초를 더해 턴 마감 6초 안", "재시도는 남은 턴 예산이 호출 상한 이상일 때만 한다. 남은 예산이 모자라면 재시도하지 않고 실패로 처리한다".
값은 측정이 아니라 잠정값이다(GPU 실측 전). 시험은 느린 가짜 추론 서버(tests/runtime_support.FlakyRuntime)로 실제 시간을 잰다.
"""
import time

import pytest
from boundary_contracts import BoundaryError

from survey_service.orchestrator import Orchestrator
from survey_service.remote import RemoteInference
from survey_service.settings import Settings

from .fakes import RecordingConsole, RecordingDispatcher
from .test_inference_client import LORA_A_FAIL, TOKEN, Env, failed
from .runtime_support import redirect_extraction


class BudgetEnv(Env):
    """Env와 같으나 추론 클라이언트는 서비스 기본(이전 설정과 같은 제어 호출 timeout 4초 하나만 줌), 턴 마감은 인자로."""
    def __init__(self, tmp, turn_deadline=6.0):
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


@pytest.fixture
def be(tmp_path):
    e = BudgetEnv(tmp_path)
    assert e.loader.poll_once().action == "LOADED"
    e.start()
    yield e
    e.close()


def test_default_settings_carry_the_contract_budget():
    s = Settings()
    assert s.inference_extract_timeout_seconds == 3.0 and s.inference_respond_timeout_seconds == 2.0
    assert s.turn_deadline_seconds == 6.0
    assert s.inference_extract_timeout_seconds + s.inference_respond_timeout_seconds < s.turn_deadline_seconds


def test_extract_is_cut_at_3_seconds_not_later(be):
    be.slow(extract=3.5)
    t0 = time.time()
    o = be.turn()
    el = time.time() - t0
    assert 2.9 < el < 3.35, el                    # 3.5초 걸리는 extract는 3.0초에서 끊긴다(이전 4초면 3.5초에 성공한다)
    assert failed(o) == LORA_A_FAIL


def test_respond_is_cut_at_2_seconds_not_later(be):
    be.slow(respond=2.5)
    t0 = time.time()
    be.turn()
    el = time.time() - t0
    assert 1.9 < el < 2.35, el                    # 2.5초 걸리는 respond는 2.0초에서 끊긴다


def test_both_calls_at_their_limits_still_fit_the_turn_deadline(be):
    be.slow(extract=2.8, respond=1.8)
    t0 = time.time()
    o = be.turn()
    el = time.time() - t0
    assert el < 6.0 and o.orchestrator_output["turn_failure_signal"] is None, el      # 최악 5.0초 + 여유 1.0초 안


def test_worst_case_with_both_calls_timing_out_is_within_6_seconds(be):
    be.slow(extract=0.1, respond=9.0)             # extract 성공 뒤 respond가 매달린다: 0.1 + 2.0
    t0 = time.time()
    be.turn()
    assert time.time() - t0 < 6.0


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
    assert be.client.stats.snapshot()["retries"] == 1                    # 남은 예산 약 6초 >= 3초: 재시도한다
    assert be.srv.calls[before:].count("extract") == 2 and o.orchestrator_output["turn_failure_signal"] is None
