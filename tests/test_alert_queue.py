"""알림 재시도 큐(L14, ADR-0058): 알림 DB에 쓰지 못한 직원 알림을 세션 DB에 남기고 복구 뒤 다시 저장·전달한다."""
import pytest

from survey_service.components.alert_book import AlertStore
from survey_service.components.alert_queue import AlertQueue, StoreError
from survey_service.components.alert_service import AlertHub, AlertService, DeliveryFailed, CURRENT_TURN

from .timing import fake_clock


class Flaky:
    """AlertStore를 감싸 create를 실패시킬 수 있게 한 대역(알림 DB 장애)."""
    def __init__(self, inner):
        self._inner, self.down = inner, False

    def create(self, *a, **k):
        if self.down:
            raise StoreError("알림 DB 장애")
        return self._inner.create(*a, **k)

    def __getattr__(self, n):
        return getattr(self._inner, n)


@pytest.fixture
def parts(tmp_path):
    clock = [1000.0]
    store = Flaky(AlertStore(str(tmp_path / "alerts.db"), fake_clock()))
    queue = AlertQueue(str(tmp_path / "session.db"), clock=lambda: clock[0])
    svc = AlertService(store, AlertHub(), push=None, monotonic=fake_clock(), queue=queue)
    yield svc, store, queue, clock
    queue.close()
    store._inner.close()


def raise_in_turn(svc, sid="S1", tid="t1"):
    tok = CURRENT_TURN.set((sid, tid))
    try:
        return svc.raise_alert(sid, "RISK", "r1", "RISK")
    finally:
        CURRENT_TURN.reset(tok)


def test_alert_that_cannot_be_stored_is_queued_then_stored_after_recovery(parts):
    svc, store, queue, clock = parts
    store.down = True
    with pytest.raises(StoreError):
        raise_in_turn(svc)
    assert queue.counts()["pending"] == 1 and svc.stats["queued"] == 1
    with pytest.raises(StoreError):
        raise_in_turn(svc)                                  # 같은 알림을 다시 넣어도 한 줄이다
    assert queue.counts()["pending"] == 1
    assert svc.drain_queue(clock[0]) == 0 and queue.counts()["pending"] == 1      # 아직 장애: 지수 백오프로 미룬다
    assert queue.due() == []
    store.down = False
    clock[0] += 1000
    assert svc.drain_queue(clock[0]) == 1 and queue.counts()["pending"] == 0
    assert store.find_by_turn("S1", "t1", "RISK_STOP") is not None             # 알림이 저장됐다(구독자가 없어 전달은 DELIVERY_FAILED로 표시된다)
    assert svc.drain_queue(clock[0]) == 0                                       # 멱등


def test_queue_is_not_used_when_the_alert_is_stored(parts):
    svc, store, queue, clock = parts
    with pytest.raises(DeliveryFailed):
        raise_in_turn(svc)                                  # 저장됐고 닿은 전달 경로가 없다
    assert queue.counts()["pending"] == 0


def test_queued_alert_is_delivered_to_subscribers_when_drained(parts):
    import queue as q
    svc, store, queue, clock = parts
    store.down = True
    with pytest.raises(StoreError):
        raise_in_turn(svc)
    sub = q.Queue()
    svc.hub.subscribe = getattr(svc.hub, "subscribe", None)
    store.down = False
    svc.hub.publish = lambda ev: (sub.put(ev), 1)[1]       # SSE 구독자 한 명
    clock[0] += 1000
    assert svc.drain_queue(clock[0]) == 1 and sub.get_nowait()["alert"]["session_id"] == "S1"
