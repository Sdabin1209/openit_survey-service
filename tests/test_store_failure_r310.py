"""RUNTIME R3.10·R3.20: 저장소 실패는 턴을 포함해 503 UNAVAILABLE(+Retry-After)이다. 턴 마감·모델 실패만 200 NOT_COMMITTED다. 상태 코드 + 본문 + 헤더를 단언한다."""
import pytest

from .e2e_support import ANSWER_OUT, Rig
from .test_public_api import call, say, start
from survey_service.components.link_public_api import PublicApi


class BrokenStore:
    """저장소 대역: 지정한 동작만 실패시키고 나머지는 실제 저장소로 넘긴다."""

    def __init__(self, inner, fail):
        self._inner, self._fail = inner, set(fail)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def load(self, sid):
        if "load" in self._fail:
            return {"session_id": sid, "status": "DB_ERROR", "revision": None, "state": None}
        return self._inner.load(sid)

    def commit(self, request, **kw):
        if "commit" in self._fail:
            return {"session_id": request["session_id"], "status": "DB_ERROR", "write_outcome": "NOT_APPLIED", "revision": None, "detail": "x"}
        return self._inner.commit(request, **kw)

    def create(self, *a, **k):
        if "create" in self._fail:
            from survey_service.wiring.errors import StoreError
            raise StoreError("쓸 수 없다")
        return self._inner.create(*a, **k)


def _api_with(fail):
    rig = Rig(risk=False)
    api = PublicApi(rig.orch, "tok", max_utterance_chars=100)
    if fail:
        rig.orch.store = BrokenStore(rig.store, fail)
    return rig, api


@pytest.mark.parametrize("fail", [("create",), ("load",)])
def test_session_start_with_an_unwritable_store_is_503_unavailable_with_retry_after(fail):
    rig, api = _api_with(fail)
    code, body, hdr = start(api)
    assert code == 503 and body["code"] == "UNAVAILABLE" and body["retryable"] is True and hdr.get("Retry-After")


def test_turn_whose_commit_fails_in_the_store_is_503_not_a_200_not_committed():
    rig, api = _api_with(())
    assert start(api)[0] == 200
    rig.orch.store = BrokenStore(rig.store, ("commit",))
    rig.model.queue.append(ANSWER_OUT)
    code, body, hdr = say(api, "안 피움", "t-1")
    assert code == 503 and body["code"] == "UNAVAILABLE" and hdr.get("Retry-After")
    rig.orch.store = rig.store
    rig.model.queue.append(ANSWER_OUT)
    code, body, _ = say(api, "안 피움", "t-1")                      # 같은 turn_id: 확정되지 않았으므로 새로 처리된다
    assert code == 200 and body["turn_failure_signal"] is None


def test_turn_deadline_is_still_200_not_committed_not_503():
    """R3.20: 턴 마감에 닿은 턴은 503이 아니라 200 NOT_COMMITTED다."""
    rig, api = _api_with(())
    start(api)
    out = rig.orch._deadline_outcome("K1")
    assert out.frontend_input["turn_failure_signal"]["write_committed"] == "NOT_COMMITTED" and out.store_failed is False
