"""시계 주입의 보조(ADR-0050). 컴포넌트와 wiring은 시간을 직접 읽지 않고 시계(`() -> float`, 단조 증가 초)를 인자로 받는다. wiring(App)이 실제 시계(time.monotonic)를 넣고, 시험은 가짜 시계를 넣어 시간을 직접 흘린다.

`join_until`은 `thread.join(timeout)`을 시계를 따르게 한 것이다. 왜 조각내서 기다리나: 전역 time을 패치하는 방법(freezegun 등)은 `Thread.join(timeout)`·`Event.wait(timeout)` 같은 블로킹 대기를 움직이지 못한다.
짧은 조각(0.05초)으로 join하며 매번 주입한 시계로 마감을 확인하면, 실제 시계에서는 원래 join과 같고 가짜 시계에서는 시험이 시계를 흘리는 순간 마감이 난다.
"""
from __future__ import annotations

import threading
from typing import Callable

SLICE_SECONDS = 0.05      # join 한 조각. 이 값은 반응 간격일 뿐 판정에 쓰이지 않는다(측정하지 않았다)


def join_until(thread: threading.Thread, until: float, now: Callable[[], float]) -> bool:
    """until(주입한 시계 기준 절대 시각)까지 thread가 끝나기를 기다린다. 끝났으면 True."""
    while thread.is_alive():
        remaining = until - now()
        if remaining <= 0:
            break
        thread.join(min(remaining, SLICE_SECONDS))
    return not thread.is_alive()
