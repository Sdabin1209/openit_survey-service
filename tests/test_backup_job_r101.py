"""RUNTIME R10.1·R9.2: 백업 잡. 마지막 줄은 BackupJobResult, 종료 코드 0·75·1. 자격·암호·접속 문자열은 어디에도 나오지 않는다.
restic은 가짜 실행기로 대신하고(우리 코드만 시험한다), 세션 DB는 실제 SQLite 파일을 쓴다."""
from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest
from boundary_contracts import validator

from survey_service.components import link_backup as backup_job

CRED, REPOPW, DBPW = "backupuser:S3CRET-cred", "S3CRET-repo", "S3CRET-db"


class FakeRestic:
    def __init__(self, fail_backup=False, no_repo=False, fail_init=False):
        self.calls, self.fail_backup, self.no_repo, self.fail_init = [], fail_backup, no_repo, fail_init

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw.get("env")))
        sub = cmd[1]
        if sub == "cat":
            return subprocess.CompletedProcess(cmd, 1 if self.no_repo else 0, "", "")
        if sub == "init":
            return subprocess.CompletedProcess(cmd, 1 if self.fail_init else 0, "", "")
        if self.fail_backup:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        out = json.dumps({"message_type": "status"}) + "\n" + json.dumps({"message_type": "summary", "snapshot_id": "abc123", "total_bytes_processed": 42})
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.fixture
def env(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))      # 덤프 임시 폴더를 시험 폴더로
    db = tmp_path / "sessions.db"
    c = sqlite3.connect(db); c.execute("CREATE TABLE t(x)"); c.execute("INSERT INTO t VALUES (1)"); c.commit(); c.close()
    (tmp_path / "cred").write_text(CRED); (tmp_path / "pw").write_text(REPOPW)
    for k in ("SURVEY_DATABASE_URL", "SURVEY_DATABASE_URL_FILE", "OPENIT_BACKUP_CREDENTIALS", "OPENIT_BACKUP_CREDENTIALS_FILE"):
        monkeypatch.delenv(k, raising=False)
    return {"OPENIT_BACKUP_URL": "https://backup.example:8000/survey-service", "OPENIT_BACKUP_CREDENTIALS_FILE": str(tmp_path / "cred"),
            "OPENIT_BACKUP_REPO_PASSWORD_FILE": str(tmp_path / "pw"), "SURVEY_SESSION_DB": str(db),
            "OPENIT_EGRESS_PROXY": "http://127.0.0.1:3128", "PATH": "/usr/bin"}


def test_sqlite_backup_is_one_snapshot_and_result_matches_the_contract(env, tmp_path):
    fake = FakeRestic()
    out, rc = backup_job.run(env, runner=fake)
    validator("BackupJobResult").validate(out)
    assert rc == 0 and out["status"] == "ok" and out["snapshot_id"] == "abc123" and out["bytes"] == 42
    cmd, e = fake.calls[-1]
    assert cmd[:3] == ["restic", "backup", "--json"] and "/.backup-" in cmd[3]
    assert e["HTTPS_PROXY"] == "http://127.0.0.1:3128"                      # 자기 영역 게이트웨이 출구로만 나간다
    assert e["RESTIC_REPOSITORY"].startswith("rest:https://") and e["RESTIC_PASSWORD_FILE"] == env["OPENIT_BACKUP_REPO_PASSWORD_FILE"]
    assert not list(tmp_path.glob(".backup-*"))                            # 임시 덤프는 지운다


def test_dump_contains_the_database_rows(env, tmp_path):
    seen = {}

    def runner(cmd, **kw):
        if cmd[1] == "backup":
            d = __import__("pathlib").Path(cmd[3])
            c = sqlite3.connect(d / "sessions.sqlite3")
            seen["rows"] = c.execute("SELECT x FROM t").fetchall(); c.close()
        return FakeRestic()(cmd, **kw)
    out, rc = backup_job.run(env, runner=runner)
    assert rc == 0 and seen["rows"] == [(1,)]


def test_missing_repository_is_initialised_once_then_backed_up(env):
    fake = FakeRestic(no_repo=True)
    out, rc = backup_job.run(env, runner=fake)
    assert rc == 0 and [c[0][1] for c in fake.calls] == ["cat", "init", "backup"]


@pytest.mark.parametrize("kw,code", [({"fail_backup": True}, 75), ({"no_repo": True, "fail_init": True}, 75)])
def test_storage_unreachable_is_exit_75_failed_and_retryable(env, kw, code):
    out, rc = backup_job.run(env, runner=FakeRestic(**kw))
    validator("BackupJobResult").validate(out)
    assert rc == code and out["status"] == "failed" and out["snapshot_id"] is None and out["error"]["retryable"] is True and out["error"]["code"] == "UNAVAILABLE"


def test_missing_configuration_is_exit_1_not_retryable(env):
    del env["OPENIT_BACKUP_URL"]
    out, rc = backup_job.run(env, runner=FakeRestic())
    validator("BackupJobResult").validate(out)
    assert rc == 1 and out["status"] == "failed" and out["error"]["retryable"] is False
    env2 = dict(env, OPENIT_BACKUP_URL="http://backup.example/x")        # https가 아니다
    assert backup_job.run(env2, runner=FakeRestic())[1] == 1


def test_postgres_dump_failure_is_retryable_and_secrets_never_appear(env, monkeypatch, tmp_path, capsys):
    (tmp_path / "dburl").write_text(f"postgresql://svc:{DBPW}@db:5432/x")
    env["SURVEY_DATABASE_URL_FILE"] = str(tmp_path / "dburl")

    def dump(url, out):
        raise RuntimeError(f"connection to {url} failed")      # 드라이버 메시지에 접속 문자열이 섞여도 결과에 싣지 않는다
    out, rc = backup_job.run(env, runner=FakeRestic(), dump_pg=dump)
    text = json.dumps(out, ensure_ascii=False)
    assert rc == 75 and out["error"]["retryable"] is True
    for secret in (CRED.split(":")[1], REPOPW, DBPW, "backup.example"):
        assert secret not in text


def test_main_prints_the_result_as_the_last_stdout_line(env, monkeypatch, capsys):
    monkeypatch.setattr(backup_job.subprocess, "run", FakeRestic())
    rc = backup_job.main(env)
    last = capsys.readouterr().out.strip().splitlines()[-1]
    validator("BackupJobResult").validate(json.loads(last))
    assert rc == 0
