"""되묻기 연속 상한의 실효 값. 입력 ReaskCapQuery -> 출력 ReaskCap(정수, 0이면 끔)."""
from __future__ import annotations

from typing import Mapping, Optional


def effective_reask_cap(max_consecutive: int, session_settings: Optional[Mapping]) -> int:
    """되묻기 연속 상한의 실효 값(0이면 끔). /readyz의 reask_limit.effective도 이 함수를 쓴다: 이웃이 규칙을 따로 계산하지 않게 한다(구조 규칙 4, ADR-0046).
    문항 세트가 정한 상한(U2) ask_limit·confirm_limit·unrecognized_streak_limit는 같은 연속 횟수에 적용하는 근사(ADR-0014)여서 가장 작은 값을 쓰고, 서비스 상한이 켜져 있으면 그것과도 min이다."""
    authored = [(session_settings or {}).get(k) for k in ("ask_limit", "confirm_limit", "unrecognized_streak_limit")]
    authored = [a for a in authored if isinstance(a, int) and not isinstance(a, bool) and a >= 1]
    if not authored:
        return max_consecutive
    return min(authored + ([max_consecutive] if max_consecutive > 0 else []))
