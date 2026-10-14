"""시험용 대역. 이웃 영역(release-gate, ops-feedback)의 실제 구현이 없어 boundary_contracts.sdk의 Protocol을 따라 직접 만든다."""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from boundary_contracts import BoundaryError
from boundary_contracts.sdk import ReleaseBundle


class RecordingConsole:
    def __init__(self, fail: bool = False) -> None:
        self.inputs: list = []
        self.fail = fail

    def deliver(self, console_input: Mapping) -> None:
        self.inputs.append(copy.deepcopy(console_input))
        if self.fail:
            raise RuntimeError("직원 콘솔 전달 실패(시험)")


class RecordingDispatcher:
    def __init__(self, output: Optional[Mapping] = None, fail: bool = False) -> None:
        self.inputs: list = []
        self.output = output or {"dispatch_record": {"succeeded": True}}
        self.fail = fail

    def dispatch(self, dispatch_input: Mapping) -> Mapping:
        self.inputs.append(copy.deepcopy(dispatch_input))
        if self.fail:
            raise RuntimeError("결과 전달 실패(시험)")
        return copy.deepcopy(self.output)


class RecordingSink:
    def __init__(self, fail: bool = False) -> None:
        self.inputs: list = []
        self.fail = fail

    def deliver(self, frontend_input: Mapping) -> None:
        self.inputs.append(copy.deepcopy(frontend_input))
        if self.fail:
            raise RuntimeError("프론트 전달 실패(시험)")


class FaultyStore:
    """진짜 저장소를 감싸 지정한 동작을 실패시킨다."""
    def __init__(self, inner, *, fail_create: bool = False, fail_commit: Optional[str] = None, fail_load: bool = False,
                 fail_append_log: bool = False) -> None:
        self._inner = inner
        self.fail_create, self.fail_commit, self.fail_load, self.fail_append_log = fail_create, fail_commit, fail_load, fail_append_log

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create(self, *a, **k):
        if self.fail_create:
            from survey_service.errors import StoreError
            raise StoreError("만들 수 없다(시험)")
        return self._inner.create(*a, **k)

    def load(self, session_id):
        if self.fail_load:
            return {"session_id": session_id, "status": "DB_ERROR", "revision": None, "state": None}
        return self._inner.load(session_id)

    def append_log(self, *a, **k):
        if self.fail_append_log:
            from survey_service.errors import StoreError
            raise StoreError("쓸 수 없다(시험)")
        return self._inner.append_log(*a, **k)

    def commit(self, request, **k):
        if self.fail_commit:
            outcome = "UNKNOWN" if self.fail_commit == "UNKNOWN" else "NOT_APPLIED"
            return {"session_id": request["session_id"], "status": "DB_ERROR", "write_outcome": outcome, "revision": None,
                    "detail": "시험용 실패"}
        return self._inner.commit(request, **k)
