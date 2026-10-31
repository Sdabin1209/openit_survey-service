"""시험의 시간 보조(U422). 시험은 실제 시간을 기다리거나 재서 판정하지 않는다.

- 우리 코드의 시간(턴 마감, /readyz 마감, 추론 재시도 예산)은 시계를 주입받으므로(ADR-0050) 시험이 FakeClock으로 시간을 직접 흘린다.
- 라이브러리 안의 시간(waitress, PostgreSQL, psycopg, 소켓 시한)은 시험하지 않는다. 넘긴 설정 값을 확인하고, 가짜 의존이 즉시 던지는 시간 초과 오류에 대한 우리 처리만 본다.
- 시험이 자기 가짜의 신호를 기다리는 대기(Event.wait, thread.join, wait_until)는 정상이면 즉시 끝난다. 코드가 진짜로 멈췄을 때만 걸리는 매달림 감시이고 숫자는 이 상수 하나다.
"""
import threading
import time

HANG_GUARD_SECONDS = 300      # 매달림 감시: 정상 경로는 수 ms이므로 수만 배의 여유다. 판정이 아니라 시험이 영영 멈추지 않게 하는 장치이며, 이 값에 기대는 단언은 없다

POLL_SECONDS = 0.05      # 반응 간격(조건을 다시 보는 주기, 시계를 흘리며 마감을 확인하는 주기). 판정에 쓰이지 않는다


class FakeClock:
    """시험이 쥔 단조 시계: 시간은 시험 코드가 advance()를 부를 때만 흐른다. 호출 가능(`clock()`)이고 sleep()은 흐르게 한다(기다리지 않는다)."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t, self._lock = start, threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


def wait_until(cond, timeout: float = HANG_GUARD_SECONDS, step: float = POLL_SECONDS) -> bool:
    """조건(가짜가 내는 신호의 결과)이 참이 될 때까지 기다린다. 정상이면 곧바로 끝난다. timeout은 매달림 감시다."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


FAKE_EPOCH = 1_700_000_000.0      # 가짜 벽시계의 시작값(고정). 실제 시계를 읽지 않는다


def fake_clock(start: float = FAKE_EPOCH) -> FakeClock:
    """컴포넌트에 넘기는 가짜 시계 하나(시험마다 실제 시계를 넣지 않는다). 시간은 시험이 advance()할 때만 간다."""
    return FakeClock(start)


def clock_kwargs() -> dict:
    """추론 클라이언트에 넘기는 단조 시계와 sleep(sleep은 같은 가짜 시계를 흘린다: 기다리지 않는다)."""
    c = FakeClock()
    return {"monotonic": c, "sleep": c.sleep}


class FakeWait:
    """주입하는 주기 대기: wait(중지 Event, 초). 실제로 기다리지 않고, 시험이 tick()을 부를 때 한 주기가 지난 것으로 돌려준다(중지 Event가 켜지면 True로 끝난다)."""

    def __init__(self) -> None:
        self._cv, self._ticks = threading.Condition(), 0

    def tick(self) -> None:
        with self._cv:
            self._ticks += 1
            self._cv.notify_all()

    def __call__(self, stop: threading.Event, seconds: float) -> bool:
        with self._cv:
            seen = self._ticks
            while self._ticks == seen and not stop.is_set():
                self._cv.wait(POLL_SECONDS)      # 반응 간격(중지 Event를 다시 보는 주기). 판정에 쓰이지 않는다
            return stop.is_set()


class FakeTimer:
    """주입하는 시한 타이머: 시험이 fire()를 부를 때만 함수를 실행한다. cancel()하면 실행하지 않는다."""

    def __init__(self, seconds: float, fn, args=()) -> None:
        self.seconds, self._fn, self._args, self.cancelled, self.started = seconds, fn, args, False, False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if self.started and not self.cancelled:
            self._fn(*self._args)
