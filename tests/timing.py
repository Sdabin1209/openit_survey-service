"""시험의 시간 상한 보조. 벽시계 상한은 서버 부하에 흔들린다(check-all이 10곳을 동시에 돌릴 때 2.42초가 나왔다).
'끊긴 시점'은 호출 경과(Stats.calls())로 재고, 남는 벽시계 상한은 그 순간의 스레드 깨어남 지연(jitter)만큼 늘린다."""
import time


def jitter(n: int = 20) -> float:
    """sleep(5ms)가 실제로 더 걸린 최대 시간(초). 부하가 없으면 1ms 안팎이다."""
    worst = 0.0
    for _ in range(n):
        t0 = time.monotonic()
        time.sleep(0.005)
        worst = max(worst, time.monotonic() - t0 - 0.005)
    return worst


def slack(base: float, factor: float = 20.0) -> float:
    """기본 여유 base에 지금 부하의 깨어남 지연 x factor를 더한다(측정 근거: 부하가 없을 때 jitter는 수 ms라 base가 그대로다)."""
    return base + factor * jitter()


def wait_until(cond, timeout: float = 10.0, step: float = 0.02) -> bool:
    """조건이 참이 될 때까지 기다린다(고정 sleep으로 '이쯤이면 됐겠지'를 추측하지 않는다). timeout은 부하가 심해도 넘지 않을 바깥 한도이고 판정은 조건이 한다."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())
