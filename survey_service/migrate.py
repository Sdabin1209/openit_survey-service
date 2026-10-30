"""스키마 버전 마이그레이션: yoyo-migrations (ADR-0030). 시작 때 직접 하던 CREATE IF NOT EXISTS / ALTER 대신 번호 붙은 마이그레이션 파일을 적용한다.

데이터베이스 종류마다 마이그레이션 디렉터리 하나(`migrations/<sqlite|postgresql>/`)이고 번호 순서가 전부다. 어느 저장소(세션, outbox, 알림)가 열든
같은 목록을 적용한다: PostgreSQL은 세 저장소가 한 데이터베이스를 쓰고, SQLite는 세션과 outbox가 한 파일을 쓰며(H02) 알림 파일에도 같은 표가 생긴다(쓰지 않는 표).
왜 저장소별 목록이 아닌가: yoyo의 내부 표(`_yoyo_log`, `_yoyo_version`)가 데이터베이스 하나에 하나씩이라 한 데이터베이스에 목록 여럿을 두면 서로 충돌한다(측정: 두 번째 목록에서 `_yoyo_log already exists`).
yoyo-migrations가 적용 기록과 동시 실행 잠금(`yoyo_lock`)을 맡는다: 서버와 보존 정리 잡이 동시에 떠도 한 번만 적용된다.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from yoyo import get_backend, read_migrations

from boundary_contracts import BoundaryError

_DIR = Path(__file__).parent / "migrations"


def migrate(kind: str, target: str) -> None:
    """`target`(SQLite 파일 경로 또는 PostgreSQL 접속 문자열)에 미적용 마이그레이션을 적용한다. 접속 문자열은 오류에 싣지 않는다."""
    if kind not in ("sqlite", "postgresql"):
        raise ValueError(f"알 수 없는 데이터베이스 종류: {kind}")
    if kind == "sqlite":
        uri = "sqlite:///" + quote(str(Path(target).resolve()), safe="/")
    else:
        uri = target.replace("postgresql://", "postgresql+psycopg://", 1).replace("postgres://", "postgresql+psycopg://", 1)
    try:
        backend = get_backend(uri)
        migrations = read_migrations(str(_DIR / kind))
        with backend.lock():
            backend.apply_migrations(backend.to_apply(migrations))
    except Exception as e:           # 접속 문자열(암호 포함 가능)이 예외 문구에 들어 있을 수 있어 클래스 이름만 남긴다
        raise BoundaryError("UNAVAILABLE", f"스키마 마이그레이션 실패: {type(e).__name__}", {"reason": "STORE_ERROR"}) from None
