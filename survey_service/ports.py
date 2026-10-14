"""Orchestrator가 기대하는 바깥 접점과 대역을 꽂는 자리(Protocol)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol


class ModelSessionLike(Protocol):
    """model-runtime의 ModelSession 중 Orchestrator가 쓰는 두 동작."""
    def parse(self, input: Mapping) -> dict: ...
    def generate(self, input: Mapping) -> dict: ...


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


@dataclass
class Engine:
    """turn-engine의 네 함수. 시험에서는 재생용 대역으로 바꾼다."""
    normalize: Callable[[Mapping], dict]
    judge: Callable[[Mapping], dict]
    write_record: Callable[[Mapping], dict]
    plan: Callable[[Mapping], dict]


def default_engine() -> Engine:
    import turn_engine
    return Engine(turn_engine.normalize, turn_engine.judge, turn_engine.write_record, turn_engine.plan)


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
    """결과 전달은 붙을 예정(COMPONENTS.yaml). 붙기 전에는 전달이 안 된 것으로 기록한다."""
    def dispatch(self, dispatch_input: Mapping) -> Mapping:
        raise RuntimeError("결과 전달이 붙어 있지 않다")
