"""wiring이 아는 오류 이름. 컴포넌트는 자기가 내는 오류를 자기 파일에 BoundaryError 하위 클래스로 정의한다(이 모듈을 import하지 않는다, DESIGN 6절).
여기서는 (1) 같은 뜻의 여러 컴포넌트 클래스를 튜플로 묶어 `except StoreError` / `pytest.raises(StoreError)`가 그대로 되게 하고,
(2) 컴포넌트가 아니라 wiring(Orchestrator)이 내는 오류를 정의한다. 모두 BoundaryError(code, details.reason)다."""
from boundary_contracts import BoundaryError

from ..components import alert_book, alert_service, extract_request, link_inference, release_registry, response_request, session_store, trace_outbox

StoreError = (session_store.StoreError, alert_book.StoreError, trace_outbox.StoreError)
StoreEffectError = session_store.StoreEffectError
StaffCallRejected = (alert_book.StaffCallRejected, alert_service.StaffCallRejected)
DeliveryFailed = alert_service.DeliveryFailed
InferenceLoadError = link_inference.InferenceLoadError
ReleaseUnavailable = release_registry.ReleaseUnavailable
TurnInputError = (extract_request.TurnInputError, response_request.TurnInputError)


class TurnIdConflict(BoundaryError):
    """같은 (세션, 턴 ID)에 다른 입력이 왔다."""
    def __init__(self, message: str = "같은 턴 ID에 다른 입력이 왔다") -> None:
        super().__init__("CONFLICT", message, {"reason": "TURN_ID_CONFLICT"})


class TurnInProgress(BoundaryError):
    """같은 (세션, 턴 ID)가 아직 처리 중이다. 새로 시작하지 않는다(FAULT_POLICY 2). API는 409 + Retry-After로 알린다."""
    def __init__(self, message: str = "같은 턴 ID가 처리 중이다") -> None:
        super().__init__("CONFLICT", message, {"reason": "TURN_IN_PROGRESS"})


class SessionNotFound(BoundaryError):
    def __init__(self, message: str = "모르는 세션이다") -> None:
        super().__init__("NOT_FOUND", message, {"reason": "SESSION_NOT_FOUND"})


class SessionEnded(BoundaryError):
    def __init__(self, message: str = "끝난 세션이다") -> None:
        super().__init__("CONFLICT", message, {"reason": "SESSION_ENDED"})
