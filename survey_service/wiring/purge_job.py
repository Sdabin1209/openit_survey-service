"""보존 기간 삭제 잡의 진입(설정 읽기, 저장소 열기, 컴포넌트 호출). 업무 규칙은 components/retention_purge.py."""
import json
import sys

from boundary_contracts import validator

from ..config import REAL_WALL_CLOCK
from ..settings import ConfigError, Settings
from ..components.retention_purge import purge, purge_session
from .db import open_stores


def run_purge(env=None, argv=None) -> int:
    argv = argv or []
    try:
        settings = Settings.from_env(env)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    try:
        store, outbox, alert_store, _kind = open_stores(settings)
    except Exception as e:
        print(f"저장소를 열 수 없다: {type(e).__name__}", file=sys.stderr)
        return 3
    try:
        if argv[:1] == ["--session"] and len(argv) == 2:
            res = purge_session(store, alert_store, argv[1])
        else:
            res = purge(store, alert_store, settings.session_retention_days, REAL_WALL_CLOCK(), outbox=outbox, access_log_days=settings.access_log_retention_days,
                        alert_days=settings.alert_retention_days, dead_outbox_days=settings.dead_outbox_retention_days)
    finally:
        store.close()
        outbox.close()
        alert_store.close()
    out = {"event": "session_purge", **res}
    errs = [e.message[:200] for e in validator("SurveyPurgeOutput").iter_errors(out)]
    if errs:                  # R7: 내보내는 쪽이 계약으로 검증한다. 어기면 표준 출력에 아무것도 내지 않고 표준 오류와 종료 코드로 알린다(값은 싣지 않는다)
        print("보존 정리 출력이 계약 SurveyPurgeOutput을 어긴다: " + "; ".join(errs[:3]), file=sys.stderr)
        return 4
    print(json.dumps(out, ensure_ascii=False))
    return 0
