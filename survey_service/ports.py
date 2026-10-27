"""Orchestrator가 기대하는 바깥 접점과 대역을 꽂는 자리(Protocol)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol


class ModelSessionLike(Protocol):
    """추론 세션 중 Orchestrator가 쓰는 두 동작. 둘 다 `boundary_contracts.clients.RuntimeResult`를 돌려주고,
    실패는 `boundary_contracts.BoundaryError`로 던진다(extract가 끝내 형식을 못 지키면 UNAVAILABLE + details.reason=OUTPUT_INVALID, D75)."""
    def extract(self, request: Mapping) -> Any: ...
    def respond(self, request: Mapping) -> Any: ...


@dataclass
class LoadedRelease:
    """적재된 릴리스. 문항 세트와 모델 세션을 가진다."""
    release_id: str
    manifest: Mapping
    bank: Mapping
    session: ModelSessionLike
    warnings: list = field(default_factory=list)


class ReleaseProvider(Protocol):
    def active(self) -> Optional[LoadedRelease]:
        """새 세션이 묶일 릴리스. 아직 없으면 None."""

    def get(self, release_id: str) -> LoadedRelease:
        """세션이 묶인 릴리스. 쓸 수 없으면 ReleaseUnavailable."""


class RiskSource(Protocol):
    """위험 신호·직원 호출 입력원(선택). v2의 A 출력(ActionExtraction)에는 위험 신호 필드가 없어 기본은 없음(None)이고 직원 알림은 비활성이다.
    입력원이 정해지면(헤드 결정, docs/change-requests/CR-012) 이 접점에 꽂는다."""
    def check(self, *, extract_input: Mapping, extract_output: Mapping, utterance: str) -> Optional[Mapping]:
        """StaffAlert(kind RISK | CALL_STAFF | USER_CALLED_STAFF)를 돌려주거나 None."""


class StaffConsole(Protocol):
    def deliver(self, console_input: Mapping) -> None:
        """StaffConsoleInput을 전달한다. 실패하면 예외."""


class ResultDispatcher(Protocol):
    def dispatch(self, dispatch_input: Mapping) -> Mapping:
        """DispatchInput을 전달하고 DispatchOutput을 돌려준다. 실패하면 예외."""


class FrontendSink(Protocol):
    """프론트 전달 자리. O06 대기: 전달 방식은 정해지지 않았다. 실패하면 예외."""
    def deliver(self, frontend_input: Mapping) -> None: ...


class TraceSink(Protocol):
    def submit(self, release_id: str, trace: Mapping) -> None:
        """TurnTrace를 봉투에 담아 내보낼 곳(영속 outbox)에 넣는다. 턴을 막지 않는다."""


class UnconfiguredStaffConsole:
    """직원 콘솔이 꽂히지 않았다. 알림이 나가지 않았으므로 성공으로 꾸미지 않고 실패한다."""
    def deliver(self, console_input: Mapping) -> None:
        raise RuntimeError("직원 콘솔이 설정되지 않았다")


class UnattachedDispatcher:
    """결과 전달은 붙을 예정(COMPONENTS.yaml): 대상이 없는 것은 정상 구성이다. graph는 attached=False를 보고 이 객체를 부르지 않고
    '결과 전달 대상 없음'으로 기록한다(전달 기록은 succeeded=false). 직접 부르면 실패한다(성공으로 꾸미지 않는다)."""
    attached = False

    def dispatch(self, dispatch_input: Mapping) -> Mapping:
        raise RuntimeError("결과 전달이 붙어 있지 않다")
