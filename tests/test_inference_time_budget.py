"""단계 B (3): 모델 호출 시간 상한 사슬 — extract 3.0초, respond 5.0초, 턴 총 마감 9.0초(ADR-0049). 재시도는 남은 턴 예산이 그 호출 상한 이상일 때만 한다.

이 파일은 시간을 재지 않는다(U421). 소켓 읽기 시한이 실제로 3.0초·5.0초에 호출을 자르는가는 계약 클라이언트·라이브러리의 몫이다. 이 영역의 책임은 둘이다:
1. 올바른 값을 넘겼는가 → 설정 기본값과 App이 클라이언트에 넘긴 값을 직접 확인한다.
2. 시간 초과가 났을 때 맞게 처리하는가 → tests/test_inference_client.py가 클라이언트가 즉시 던지는 시간 초과 오류로 본다(단계 실패, 재시도 없음).
재시도 판단은 우리 코드(남은 예산과 호출 상한의 비교)라 시계를 주입해 결정적으로 본다: 마감 9.0초(주입한 시계)면 재시도하고 호출 상한(3.0)보다 작은 2.0초면 재시도하지 않는다.
"""
import pytest
from boundary_contracts import BoundaryError

from survey_service.wiring.orchestrator import Orchestrator
from survey_service.components.link_inference import RemoteInference
from survey_service.settings import Settings

from .fakes import RecordingConsole, RecordingDispatcher
from .test_inference_client import TOKEN, Env
from .timing import FakeClock, HANG_GUARD_SECONDS


class BudgetEnv(Env):
    """Env와 같으나 추론 클라이언트는 서비스 기본 호출 상한(extract 3.0, respond 5.0)을 쓰고 턴 마감은 인자로 준다. 가짜 서버가 즉시 답하므로 상한이 결과를 바꾸지 않는다."""
    def __init__(self, tmp, turn_deadline=9.0):
        super().__init__(tmp)
        s = Settings()
        self.clock = FakeClock()          # 주입한 시계(ADR-0050): 재시도 예산과 백오프 대기가 실제 시간을 쓰지 않는다
        self.client = RemoteInference(self.url, TOKEN, HANG_GUARD_SECONDS, load_timeout=HANG_GUARD_SECONDS, retries=1, poll_interval=0.05, backoff=0.01,
                                      extract_timeout=s.inference_extract_timeout_seconds, respond_timeout=s.inference_respond_timeout_seconds,
                                      monotonic=self.clock, sleep=self.clock.sleep)
        self.loader.inference = self.client
        self.orch = Orchestrator(self.orch.store, self.registry, staff_console=RecordingConsole(), dispatcher=RecordingDispatcher(),
                                 turn_deadline_seconds=turn_deadline, clock=self.clock)


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


def test_the_app_passes_the_per_call_limits_from_settings_to_the_inference_client(tmp_path, monkeypatch):
    """App이 만든 추론 클라이언트의 호출 종류별 상한이 설정 값이다(시간을 재지 않고 넘긴 값을 본다)."""
    from survey_service.wiring.app import App
    from .fakes import FakeReleaseReader, FakeTraceIngest
    env = {"SURVEY_WORK_DIR": str(tmp_path / "w"), "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_INFERENCE_URL": "http://127.0.0.1:9",
           "SURVEY_SESSION_IDLE_SECONDS": "0", "SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"),
           "SURVEY_INFERENCE_EXTRACT_TIMEOUT_SECONDS": "1.5", "SURVEY_INFERENCE_RESPOND_TIMEOUT_SECONDS": "2.5"}
    monkeypatch.setenv("SURVEY_INFERENCE_TOKEN", TOKEN)
    app = App(Settings.from_env(env), reader=FakeReleaseReader(tmp_path / "gate"), ingest=FakeTraceIngest())
    try:
        assert app.inference._op_rts["extract"]._timeout == 1.5 and app.inference._op_rts["respond"]._timeout == 2.5
    finally:
        app.stop()


def test_no_retry_when_remaining_turn_budget_is_below_the_call_limit(tmp_path):
    e = BudgetEnv(tmp_path, turn_deadline=2.0)    # 남은 예산 2.0초(주입한 시계는 시험이 흘릴 때만 간다) < extract 상한 3.0초
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


def test_retry_happens_when_remaining_turn_budget_covers_the_call_limit(tmp_path):
    e = BudgetEnv(tmp_path, turn_deadline=9.0)      # 남은 예산 9.0초 >= extract 상한 3.0초
    try:
        assert e.loader.poll_once().action == "LOADED"
        e.start()
        e.srv.fail_next = [BoundaryError("UNAVAILABLE", "일시 장애")]
        before = len(e.srv.calls)
        o = e.turn()
        assert e.client.stats.snapshot()["retries"] == 1
        assert e.srv.calls[before:].count("extract") == 2 and o.orchestrator_output["turn_failure_signal"] is None
    finally:
        e.close()
