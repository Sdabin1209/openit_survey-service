"""백업 잡(계약 RUNTIME R10.1·R9.2, BackupJobResult): 이 영역의 데이터베이스를 꺼내 자기 영역 게이트웨이 출구로 platform 백업 저장소(restic REST)에 보낸다.

왜 restic: 백업 저장소 프로토콜이 계약 CommonEnv에 'restic REST 서버'(OPENIT_BACKUP_URL)로 정해져 있고 restic이 그 표준 클라이언트다(ADR-0060).
왜 COPY·sqlite3 백업 API: 실행 중인 DB를 일관된 한 시점으로 꺼내는 각 엔진의 표준 방법이다. pg_dump가 아닌 이유: pg_dump는 서버 주 버전 이상이어야 하는데 이미지의 배포판 패키지 버전이 서버(PostgreSQL 16)와 어긋날 수 있고,
psycopg는 이미 의존이며 COPY는 서버 버전에 묶이지 않는다. 복원은 마이그레이션으로 표를 만든 뒤 같은 이름의 파일을 COPY ... FROM으로 넣는다(docs/runbook.md).
비밀(접속 문자열·백업 자격·저장소 암호)은 파일로 받고 어떤 출력·로그에도 쓰지 않는다(D84). 멱등: 다시 실행하면 새 스냅숏이 하나 더 생길 뿐 이전 것을 바꾸지 않는다.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from boundary_contracts import BoundaryError, read_secret, validator
from boundary_contracts.clients import egress_proxy_from

EX_TEMPFAIL = 75      # 재시도 가능(BackupJobResult: 백업 저장소·DB에 닿지 못함)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_url(url: str, credentials: str) -> str:
    """restic REST 주소 rest:https://<사용자>:<암호>@호스트/경로 (restic 문서의 표준 형식, RFC 7617 자격). https만 허용한다."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise BoundaryError("INVALID_RECORD", "OPENIT_BACKUP_URL은 https 주소여야 한다")
    user, _, pw = credentials.strip().partition(":")
    netloc = f"{quote(user, safe='')}:{quote(pw, safe='')}@{parts.netloc}"
    return "rest:" + urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def _dump_sqlite(db: Path, out: Path) -> None:
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)      # sqlite3 온라인 백업 API: 쓰는 중에도 일관된 사본
        finally:
            dst.close()
    finally:
        src.close()


def _dump_postgres(url: str, out: Path) -> None:
    """모든 표를 COPY ... TO STDOUT (FORMAT binary)로 파일마다 꺼낸다. 한 REPEATABLE READ 트랜잭션이라 표 사이가 일관된 한 시점이다."""
    import psycopg
    with psycopg.connect(url, autocommit=False) as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        names = [r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' ORDER BY 1")]
        for name in names:
            with open(out / f"{name}.copy", "wb") as f, c.cursor().copy(f'COPY "{name}" TO STDOUT (FORMAT binary)') as cp:
                for chunk in cp:
                    f.write(chunk)


def run(environ, *, runner: Optional[Callable] = None, now: Callable[[], str] = _ts, dump_pg: Callable = _dump_postgres) -> tuple:
    """(BackupJobResult 레코드, 종료 코드). 종료 코드 0 ok, 75 재시도 가능(백업 저장소·DB에 닿지 못함), 1 그 밖의 실패."""
    runner = runner or subprocess.run
    started = now()
    try:
        url = environ.get("OPENIT_BACKUP_URL")
        if not url:
            raise BoundaryError("INVALID_RECORD", "OPENIT_BACKUP_URL이 필요하다")
        creds = read_secret("OPENIT_BACKUP_CREDENTIALS", environ=environ)
        pw_file = environ.get("OPENIT_BACKUP_REPO_PASSWORD_FILE")
        if not creds or not pw_file:
            raise BoundaryError("INVALID_RECORD", "OPENIT_BACKUP_CREDENTIALS_FILE과 OPENIT_BACKUP_REPO_PASSWORD_FILE이 필요하다")
        db_url = read_secret("SURVEY_DATABASE_URL", environ=environ)
        session_db = environ.get("SURVEY_SESSION_DB") or "/data/sessions.db"
        proxy = egress_proxy_from(environ)
        env = {"PATH": environ.get("PATH", ""), "RESTIC_REPOSITORY": _repo_url(url, creds), "RESTIC_PASSWORD_FILE": pw_file}
        if "SSL_CERT_FILE" in environ:
            env["SSL_CERT_FILE"] = environ["SSL_CERT_FILE"]      # 사설 루트를 표준 변수로 믿는다(Go 표준 라이브러리)
        if proxy:
            env["HTTPS_PROXY"] = proxy                            # 자기 영역 게이트웨이의 CONNECT 출구(R1.8). restic은 표준 HTTPS_PROXY를 읽는다
        stage = Path(tempfile.mkdtemp(prefix=".backup-", dir=environ.get("SURVEY_BACKUP_TMP") or tempfile.gettempdir()))
        try:
            if db_url:
                try:
                    dump_pg(db_url, stage)
                except Exception as e:      # noqa: BLE001 - 접속 정보가 섞일 수 있어 종류만 남긴다
                    raise BoundaryError("UNAVAILABLE", f"DB 내보내기에 실패했다({type(e).__name__})") from None
            else:
                _dump_sqlite(Path(session_db), stage / "sessions.sqlite3")
            # 저장소가 없으면 만든다(멱등: 이미 있으면 cat config가 성공한다)
            if runner(["restic", "cat", "config"], capture_output=True, text=True, env=env).returncode != 0:
                init = runner(["restic", "init"], capture_output=True, text=True, env=env)
                if init.returncode != 0:
                    raise BoundaryError("UNAVAILABLE", "백업 저장소에 닿지 못했다")
            r = runner(["restic", "backup", "--json", str(stage)], capture_output=True, text=True, env=env)
            if r.returncode != 0:
                raise BoundaryError("UNAVAILABLE", "restic backup이 실패했다")
            summary = None
            for line in r.stdout.splitlines():
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if j.get("message_type") == "summary":
                    summary = j
            if summary is None:
                raise BoundaryError("INVALID_RECORD", "restic이 요약을 내지 않았다")
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        out = {"status": "ok", "snapshot_id": str(summary["snapshot_id"])[:128], "bytes": int(summary.get("total_bytes_processed", 0)),
               "started_at": started, "finished_at": now()}
        return out, 0
    except BoundaryError as e:
        # 메시지는 우리가 쓴 고정 문구라 비밀이 없다(restic·DB 드라이버의 출력은 싣지 않는다)
        err = {"code": e.code, "message": e.message, "retryable": e.retryable}
        return ({"status": "failed", "snapshot_id": None, "bytes": 0, "started_at": started, "finished_at": now(), "error": err},
                EX_TEMPFAIL if e.retryable else 1)
    except Exception as e:      # noqa: BLE001 - 종류만 싣는다(예외 문구에 접속 정보가 섞일 수 있다)
        err = {"code": "UNAVAILABLE", "message": f"예기치 못한 예외: {type(e).__name__}", "retryable": True}
        return {"status": "failed", "snapshot_id": None, "bytes": 0, "started_at": started, "finished_at": now(), "error": err}, EX_TEMPFAIL


def main(environ=None) -> int:
    """`python -m survey_service backup`: 마지막 줄 JSON(BackupJobResult), 종료 코드 0·75·1. 내보내기 전에 계약으로 검증한다(R7)."""
    out, rc = run(os.environ if environ is None else environ)
    validator("BackupJobResult").validate(out)
    print(json.dumps(out, ensure_ascii=False))
    return rc
