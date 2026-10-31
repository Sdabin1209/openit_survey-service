"""조립에서 컴포넌트에 넘기는 실제 시계·타이머(ADR-0050). 컴포넌트는 `time`도 이 모듈도 읽지 않고(tests/test_structure.py가 강제) 시계·타이머를 필수 인자로 받는다.
wiring(App, db.open_stores, purge_job)과 진입점(cli)이 여기의 실제 값을 명시적으로 넘기고, 시험은 tests/timing.py의 가짜를 넘긴다.
"""
import threading
import time

REAL_WALL_CLOCK = time.time           # 벽시계(epoch 초): 저장 시각, 전송 시각처럼 기록에 남는 값
REAL_MONOTONIC = time.monotonic       # 단조 시계: 마감·예산·간격
REAL_SLEEP = time.sleep


def real_wait(stop: threading.Event, seconds: float) -> bool:
    """주기 대기: 중지 Event가 켜지거나 seconds가 지날 때까지. 중지됐으면 True."""
    return stop.wait(seconds)


def real_timer(seconds: float, fn, args=()):
    """문장 강제 끊기 같은 시한 타이머. start()·cancel()을 가진 데몬 타이머."""
    t = threading.Timer(seconds, fn, args=args)
    t.daemon = True
    return t
