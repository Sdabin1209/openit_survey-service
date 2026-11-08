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
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from boundary_contracts import BoundaryError, read_secret, secret_format_problems, validator
from boundary_contracts.clients import egress_proxy_from, require_tls_url

EX_TEMPFAIL = 75      # 재시도 가능(BackupJobResult: 백업 저장소·DB에 닿지 못함)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_url(url: str, credentials: str) -> str:
    """restic REST 주소 rest:https://<사용자>:<암호>@호스트/경로 (restic 문서의 표준 형식, RFC 7617 자격). https만 허용한다."""
    parts = urlsplit(url)
    try:
        require_tls_url(url)      # R1.1: https만, loopback만 http 허용
    except ValueError:
        raise BoundaryError("INVALID_RECORD", "OPENIT_BACKUP_URL은 https 주소여야 한다(loopback만 http)") from None
    if not parts.hostname:
        raise BoundaryError("INVALID_RECORD", "OPENIT_BACKUP_URL이 주소가 아니다")
    user, _, pw = credentials.strip().partition(":")
    netloc = f"{quote(user, safe='')}:{quote(pw, safe='')}@{parts.netloc}"
    return "rest:" + urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def _restic_env(environ, url: str, creds: str, pw_file: str) -> dict:
    """restic에 주는 환경(필요한 것만, R9.1). HOME이 없는 비root 사용자(이미지의 app)에서도 돌도록 캐시 폴더를 임시 폴더 아래로 정한다(restic은 캐시 폴더를 못 찾으면 실패한다)."""
    env = {"PATH": environ.get("PATH", ""), "RESTIC_REPOSITORY": _repo_url(url, creds), "RESTIC_PASSWORD_FILE": pw_file,
           "RESTIC_CACHE_DIR": os.path.join(tempfile.gettempdir(), ".restic-cache")}
    return env


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
        # 왜: 비밀 파일 내용 형식의 정본은 계약 secret_format_problems 다(5.0.1, D117). 어기면 값은 싣지 않고 사유 코드만 낸다. 형식이 틀린 자격 증명으로 restic 을 부르지 않는다
        bad = secret_format_problems("credentials_line", creds)
        if bad:
            raise BoundaryError("INVALID_RECORD", "OPENIT_BACKUP_CREDENTIALS 형식이 아니다(user:password 한 줄)", {"reason": bad[0]})
        db_url = read_secret("SURVEY_DATABASE_URL", environ=environ)
        session_db = environ.get("SURVEY_SESSION_DB") or "/data/sessions.db"
        proxy = egress_proxy_from(environ)
        env = _restic_env(environ, url, creds, pw_file)
        if "SSL_CERT_FILE" in environ:
            env["SSL_CERT_FILE"] = environ["SSL_CERT_FILE"]      # 사설 루트를 표준 변수로 믿는다(Go 표준 라이브러리)
        if proxy:
            env["HTTPS_PROXY"] = proxy                            # 자기 영역 게이트웨이의 CONNECT 출구(R1.8). restic은 표준 HTTPS_PROXY를 읽는다
        stage = Path(tempfile.mkdtemp(prefix=".backup-"))      # 표준 임시 폴더(TMPDIR). 새 설정 이름을 만들지 않는다
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
            keep_days = int(environ.get("SURVEY_SESSION_RETENTION_DAYS") or 90)      # 보존 상한: 세션 데이터 보존 기간 이하(ADR-0062). 기본은 계약 SurveyServiceEnv의 90일
            fg = runner(["restic", "forget", "--prune", "--keep-within", f"{keep_days}d"], capture_output=True, text=True, env=env)
            if fg.returncode != 0:
                raise BoundaryError("UNAVAILABLE", "restic forget --prune이 실패했다")
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


# ---------------------------------------------------------------- 복구 잡 (계약 DATA_OWNERSHIP 백업 행: 복구는 그 영역의 복구 잡)
_SKIP_TABLES = ("_yoyo", "yoyo")      # 마이그레이션 기록 표: 복구는 마이그레이션으로 표를 먼저 만들고 그 기록은 그대로 둔다


def _load_postgres(url: str, src: Path) -> int:
    """빈 DB에 표별 `.copy`를 COPY ... FROM으로 넣는다(한 트랜잭션: 하나라도 실패하면 아무것도 넣지 않는다). 표가 비어 있지 않으면 거부한다(복구는 빈 DB에만). 행 수를 돌려준다."""
    import psycopg
    files = sorted(src.glob("*.copy"))
    if not files:
        raise BoundaryError("INVALID_RECORD", "복구할 덤프 파일이 스냅숏에 없다")
    total = 0
    with psycopg.connect(url, autocommit=False) as c:
        names = [f.stem for f in files if not f.stem.startswith(_SKIP_TABLES)]
        for name in names:
            if c.execute(f'SELECT EXISTS (SELECT 1 FROM "{name}")').fetchone()[0]:
                raise BoundaryError("FAILED_PRECONDITION", "복구 대상 데이터베이스가 비어 있지 않다")
        for name in names:
            with open(src / f"{name}.copy", "rb") as f, c.cursor().copy(f'COPY "{name}" FROM STDIN (FORMAT binary)') as cp:
                while chunk := f.read(1 << 20):
                    cp.write(chunk)
            total += c.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for (col,) in c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s", (name,)).fetchall():
                seq = c.execute("SELECT pg_get_serial_sequence(%s, %s)", (f'"{name}"', col)).fetchone()[0]
                if seq:      # 표가 소유한 순번은 복구한 최댓값 다음부터 이어 간다
                    c.execute(f'SELECT setval(%s, COALESCE((SELECT max("{col}") FROM "{name}"), 1), (SELECT max("{col}") FROM "{name}") IS NOT NULL)', (seq,))
        if "sessions" in names:      # 결과 목록 순서·커서의 번호(update_seq)는 표 밖의 순번이다(ADR-0034)
            c.execute("SELECT setval('sessions_update_seq', COALESCE((SELECT max(update_seq) FROM sessions), 1), (SELECT max(update_seq) FROM sessions) IS NOT NULL)")
        c.commit()
    return total


def _load_sqlite(src: Path, dest: Path) -> int:
    f = src / "sessions.sqlite3"
    if not f.exists():
        raise BoundaryError("INVALID_RECORD", "복구할 SQLite 덤프가 스냅숏에 없다")
    if dest.exists() and dest.stat().st_size > 0:
        raise BoundaryError("FAILED_PRECONDITION", "복구 대상 파일이 비어 있지 않다")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(f, dest)
    return dest.stat().st_size


def restore(environ, *, snapshot: str = "latest", runner: Optional[Callable] = None, now: Callable[[], str] = _ts, load_pg: Callable = _load_postgres) -> tuple:
    """(BackupJobResult 레코드, 종료 코드). 스냅숏 ID 또는 latest를 빈 DB로 복구한다. 종료 코드 0 ok, 75 재시도 가능, 1 그 밖의 실패(빈 DB가 아님 포함)."""
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
        if not (snapshot == "latest" or (snapshot.isalnum() and 4 <= len(snapshot) <= 64)):
            raise BoundaryError("INVALID_RECORD", "스냅숏은 ID 또는 latest여야 한다")
        db_url = read_secret("SURVEY_DATABASE_URL", environ=environ)
        proxy = egress_proxy_from(environ)
        env = _restic_env(environ, url, creds, pw_file)
        if "SSL_CERT_FILE" in environ:
            env["SSL_CERT_FILE"] = environ["SSL_CERT_FILE"]
        if proxy:
            env["HTTPS_PROXY"] = proxy
        stage = Path(tempfile.mkdtemp(prefix=".restore-"))
        try:
            r = runner(["restic", "restore", snapshot, "--target", str(stage), "--json"], capture_output=True, text=True, env=env)
            if r.returncode != 0:
                raise BoundaryError("UNAVAILABLE", "restic restore가 실패했다(스냅숏이 없거나 백업 저장소에 닿지 못했다)")
            summary = None
            for line in r.stdout.splitlines():
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if j.get("message_type") == "summary":
                    summary = j
            dumps = sorted({p.parent for p in stage.rglob("*.copy")} | {p.parent for p in stage.rglob("sessions.sqlite3")})
            if len(dumps) != 1:
                raise BoundaryError("INVALID_RECORD", "스냅숏에서 덤프 폴더를 하나로 찾지 못했다")
            if db_url:
                try:
                    rows = load_pg(db_url, dumps[0])
                except BoundaryError:
                    raise
                except Exception as e:      # noqa: BLE001 - 접속 정보가 섞일 수 있어 종류만 남긴다
                    raise BoundaryError("UNAVAILABLE", f"DB 복구에 실패했다({type(e).__name__})") from None
            else:
                rows = _load_sqlite(dumps[0], Path(environ.get("SURVEY_SESSION_DB") or "/data/sessions.db"))
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        sid = str((summary or {}).get("snapshot_id") or snapshot)[:128]
        return {"status": "ok", "snapshot_id": sid, "bytes": int(rows), "started_at": started, "finished_at": now()}, 0
    except BoundaryError as e:
        err = {"code": e.code, "message": e.message, "retryable": e.retryable}
        return ({"status": "failed", "snapshot_id": None, "bytes": 0, "started_at": started, "finished_at": now(), "error": err},
                EX_TEMPFAIL if e.retryable else 1)
    except Exception as e:      # noqa: BLE001
        err = {"code": "UNAVAILABLE", "message": f"예기치 못한 예외: {type(e).__name__}", "retryable": True}
        return {"status": "failed", "snapshot_id": None, "bytes": 0, "started_at": started, "finished_at": now(), "error": err}, EX_TEMPFAIL


def restore_main(argv=None, environ=None) -> int:
    """`python -m survey_service restore [--snapshot <ID|latest>]`: 마지막 줄 JSON(BackupJobResult), 종료 코드 0·75·1. 주기 없이 사람이나 복구 절차가 한 번 띄운다."""
    argv = list(argv or [])
    snap = "latest"
    if argv[:1] == ["--snapshot"] and len(argv) == 2:
        snap = argv[1]
    elif argv:
        print("사용: python -m survey_service restore [--snapshot <ID|latest>]", file=sys.stderr)
        return 2
    out, rc = restore(os.environ if environ is None else environ, snapshot=snap)
    validator("BackupJobResult").validate(out)
    print(json.dumps(out, ensure_ascii=False))
    return rc
