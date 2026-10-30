"""보존 기간 정리(run-once 잡 `python -m survey_service purge`, 서비스 안의 주기 작업도 같은 함수를 쓴다). 모든 기간은 설정이고 기본값이 있으며 무기한은 거부한다(D88 U4, ADR-0015).

- 세션: 끝난(ENDED_*) 세션 중 마지막 기록이 `SURVEY_SESSION_RETENTION_DAYS`(기본 90)보다 오래된 것의 세션 문서·턴 기록·환자 참조·시각 기록·위험 신호 기록. 진행·정지 중인 세션은 지우지 않는다.
- 직원 접근 기록(staff_access_log) `SURVEY_ACCESS_LOG_RETENTION_DAYS`(기본 365), 해결된 알림 `SURVEY_ALERT_RETENTION_DAYS`(기본 365, 해결 시각 기준, 열린 알림은 지우지 않음),
  DEAD로 끝난 Trace outbox 행 `SURVEY_DEAD_OUTBOX_RETENTION_DAYS`(기본 30). 알림 이벤트는 최근 1만 건만 남긴다(서비스 주기 작업).
- 세션 한 건 삭제(환자 요청 등): `python -m survey_service purge --session <id>`(끝난 세션이 아니어도 지운다. 열린 알림은 남는다).
- 멱등: 같은 입력으로 여러 번 돌려도 안전하다(알림 DB 쪽을 먼저 지우므로 중간에 죽어도 다음 실행이 이어서 지운다).
- 로그에는 지운 개수만 남긴다(세션 ID·환자 참조 없음).
"""
from __future__ import annotations

import logging
import time
from typing import Optional


log = logging.getLogger("survey_service.retention")
BATCH = 200


def purge(store, alert_store, retention_days: Optional[int], now: Optional[float] = None, *, outbox=None, access_log_days: Optional[int] = None,
          alert_days: Optional[int] = None, dead_outbox_days: Optional[int] = None) -> dict:
    now = time.time() if now is None else now
    res: dict = {"configured": retention_days is not None, "purged_sessions": 0}
    if retention_days is not None:
        res["retention_days"] = retention_days
        ids = store.list_ended_before(now - retention_days * 86400)
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            alert_store.purge_sessions(chunk)
            res["purged_sessions"] += store.delete_sessions(chunk)
    if access_log_days is not None:
        res["purged_access_log"] = alert_store.purge_access_log(now - access_log_days * 86400)
    if alert_days is not None:
        res["purged_alerts"] = alert_store.purge_resolved_alerts(now - alert_days * 86400)
    if dead_outbox_days is not None and outbox is not None:
        res["purged_dead_outbox"] = outbox.purge_dead(now - dead_outbox_days * 86400)
    return res


def purge_session(store, alert_store, session_id: str) -> dict:
    """세션 한 건을 지운다(세션 문서·턴 기록·환자 참조·시각·위험 신호 기록). 접근 기록·알림 행은 자기 보존 기간을 따른다."""
    alert_store.purge_sessions([session_id])
    return {"purged_sessions": store.delete_sessions([session_id])}
