"""운영 소요 시간 지표(U71, 계약 METRICS.md "운영 소요 시간 지표", ADR-0055). 입력: 턴·모델 호출 하나의 (결과 이름, 경과 초) -> 출력: Prometheus 텍스트.

지표 두 개(이름과 라벨은 계약이 정한다): `openit_turn_duration_seconds{outcome=ok|failed|paused}`, `openit_model_call_duration_seconds{op=extract|respond,outcome=ok|invalid|timeout|unavailable}`.
값을 모으기만 한다. 목표(p95 2.0초 등) 판정은 하지 않는다(경보 규칙은 deploy의 몫).
"""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Histogram, generate_latest

# 왜 이 버킷인가: 목표(SLO)의 경계(턴 2.0초, 모델 호출 합 1.5초)와 실패 판정 상한(extract 21, respond 26, 턴 50: 계약 CONVENTIONS 시간 상한 사슬, D125)을
# 버킷 경계에 두어 "목표를 넘은 비율"과 "상한에 닿은 비율"을 히스토그램에서 바로 읽게 한다. 사이는 대략 두 배씩. 실제 분포는 측정하지 않았다(ADR-0055).
TURN_BUCKETS = (0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 20.0, 50.0)
MODEL_CALL_BUCKETS = (0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 5.0, 10.0, 21.0, 26.0)

TURN_OUTCOMES = ("ok", "failed", "paused")
MODEL_OPS = ("extract", "respond")
MODEL_OUTCOMES = ("ok", "invalid", "timeout", "unavailable")


class Metrics:
    """프로세스마다 하나. 자기 레지스트리를 갖는다(전역 기본 레지스트리를 쓰면 같은 프로세스의 둘째 App이 이름 중복으로 실패한다)."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self._turn = Histogram("openit_turn_duration_seconds", "환자 발화 접수부터 응답 확정까지(서버 쪽)", ["outcome"], buckets=TURN_BUCKETS, registry=self.registry)
        self._model = Histogram("openit_model_call_duration_seconds", "모델 호출 하나의 클라이언트 쪽 소요(요청 전송~응답 수신)", ["op", "outcome"],
                                buckets=MODEL_CALL_BUCKETS, registry=self.registry)
        for o in TURN_OUTCOMES:      # 한 번도 안 나온 결과도 0으로 보이게 한다: 시계열이 없다와 0건이다를 구분하려고
            self._turn.labels(o)
        for op in MODEL_OPS:
            for o in MODEL_OUTCOMES:
                self._model.labels(op, o)

    def observe_turn(self, outcome: str, seconds: float) -> None:
        if outcome in TURN_OUTCOMES:
            self._turn.labels(outcome).observe(max(0.0, seconds))

    def observe_model_call(self, op: str, outcome: str, seconds: float) -> None:
        if op in MODEL_OPS and outcome in MODEL_OUTCOMES:
            self._model.labels(op, outcome).observe(max(0.0, seconds))

    def render(self) -> tuple[bytes, str]:
        """(본문, Content-Type). 개인정보·세션 ID·발화는 라벨에도 값에도 없다."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


def turn_outcome(frontend_input: dict | None) -> str:
    """턴 결과 이름. 실패 신호가 없으면 ok, 직원 알림이 닿았거나 정지 사유가 있는 신호면 paused, 그 밖의 신호는 failed."""
    sig = (frontend_input or {}).get("turn_failure_signal")
    if not sig:
        return "ok"
    return "paused" if sig.get("staff_alert") == "DELIVERED" or sig.get("pause_reason") else "failed"
