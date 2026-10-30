"""처리 표(9-1): 입력 종류와 세션 상태로 이 턴의 처리 경로를 고른다. 입력 TurnRouteQuery -> 출력 TurnRoute."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Route:
    """처리 표(9-1)의 한 칸."""
    turn_kind: Optional[str]      # None이면 처리 안 함
    create: bool = False          # 세션 정보 생성
    full: bool = False            # LoRA-A~저장 판단까지 거치는 전체 흐름
    keep_log: bool = True         # 처리 안 함일 때 대화 로그를 남기는가


_IGNORE_NOLOG = Route(None, keep_log=False)
_IGNORE_LOG = Route(None, keep_log=True)

# 처리 표 9-1: 열 = 세션 상태, 행 = 입력 종류(SESSION_START | UTTERANCE | RESUME | END | SESSION_END)
COLUMNS: dict[str, dict[str, Route]] = {
    "none": {"SESSION_START": Route("SESSION_START", create=True), "UTTERANCE": Route("UTTERANCE", create=True, full=True)},
    "progress": {"SESSION_START": Route("SESSION_START"), "UTTERANCE": Route("UTTERANCE", full=True),
                 "END": Route("INTERVENTION_END"), "SESSION_END": Route("END_LEFT"),
                 "RESUME": _IGNORE_LOG},                      # 진행 중 세션에 RESUME: 뜻이 없다(G6)
    "paused": {"RESUME": Route("RESUME"), "END": Route("INTERVENTION_END"), "SESSION_END": Route("END_LEFT"),
               "SESSION_START": _IGNORE_LOG, "UTTERANCE": _IGNORE_LOG},   # 멈춘 동안의 말은 직원이 확인할 수 있게 남긴다(G5)
    "ended": {},
}
_DEFAULT = {"none": _IGNORE_NOLOG, "progress": _IGNORE_LOG, "paused": _IGNORE_LOG, "ended": _IGNORE_NOLOG}


def column_of(found: bool, status: Optional[str]) -> str:
    if not found:
        return "none"
    if status and status.startswith("ENDED"):
        return "ended"
    return "paused" if status == "PAUSED_RISK" else "progress"


def route(input_kind: str, session_found: bool, status: Optional[str]) -> Route:
    col = column_of(session_found, status)
    return COLUMNS[col].get(input_kind, _DEFAULT[col])
