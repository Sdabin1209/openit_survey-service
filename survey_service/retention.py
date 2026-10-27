"""세션 보존 기간 정리(run-once 잡): `python -m survey_service purge`.

- `SURVEY_SESSION_RETENTION_DAYS`가 없으면(기본) 아무것도 지우지 않는다. 값이 있으면 끝난(ENDED_*) 세션 중 마지막 기록이 그보다 오래된 것을 지운다.
  진행 중·정지 중인 세션은 지우지 않는다.
- 지우는 것: 세션 문서와 턴 기록, 환자 참조(session_meta), 시각 기록(session_timeline), 위험 신호 기록. 지우지 않는 것: 접근 기록(staff_access_log),
  알림 행(세션 ID만 담는다), Trace outbox(자기 보존 기간 설정을 따른다).
- 멱등: 같은 입력으로 여러 번 돌려도 안전하다(알림 DB 쪽을 먼저 지우므로 중간에 죽어도 다음 실행이 이어서 지운다). 언제 돌릴지는 deploy의 스케줄러가 정한다.
- 로그에는 지운 개수만 남긴다(세션 ID·환자 참조 없음).
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Optional

from .alerts import AlertStore
from .settings import ConfigError, Settings
from .store.sqlite import SqliteSessionStore

log = logging.getLogger("survey_service.retention")
BATCH = 200


def purge(store, alert_store, retention_days: Optional[int], now: Optional[float] = None) -> dict:
    if retention_days is None:
        return {"configured": False, "purged_sessions": 0}
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    ids = store.list_ended_before(cutoff)
    purged = 0
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        alert_store.purge_sessions(chunk)
        purged += store.delete_sessions(chunk)
    return {"configured": True, "retention_days": retention_days, "purged_sessions": purged}


def run_purge(env=None) -> int:
    try:
        settings = Settings.from_env(env)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    store = SqliteSessionStore(settings.session_db)
    alert_store = AlertStore(settings.alert_db_path())
    try:
        res = purge(store, alert_store, settings.session_retention_days)
    finally:
        store.close()
        alert_store.close()
    print(json.dumps({"event": "session_purge", **res}, ensure_ascii=False))
    return 0
