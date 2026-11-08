"""db/initdb.d/10-areadb.sh: 계정 암호가 psql 의 명령행 인자(프로세스 목록에 보인다)에 나타나지 않는다. 가짜 psql 이 인자와 표준 입력을 기록한다(docker 불필요).
왜: 예전 구현은 psql -v pw=<암호> 로 넘겨 ps 에 암호가 보였다(2026-11 전수 점검). 암호는 표준 입력의 메타명령(\\set pw `head ...`)으로만 읽는다."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "db" / "initdb.d" / "10-areadb.sh"
SECRET = "Zq9-unique-secret-value-77"


def test_password_not_in_psql_argv(tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "calls.log"
    fake = bin_dir / "psql"
    fake.write_text(f'#!/bin/bash\necho "ARGV: $*" >> "{log}"\ncat >> "{log}"\n', encoding="utf-8")
    fake.chmod(0o755)
    sec = tmp_path / "sec"; sec.mkdir()
    (sec / "svc_owner").write_text(SECRET + "\n", encoding="utf-8")
    accounts = tmp_path / "accounts.tsv"
    accounts.write_text("svc_owner\tsvcdb\towner\n", encoding="utf-8")
    script = tmp_path / "run.sh"      # 고정 경로 /etc/openit/accounts.tsv 만 시험용 경로로 바꾼다
    script.write_text(SCRIPT.read_text(encoding="utf-8").replace("/etc/openit/accounts.tsv", str(accounts)), encoding="utf-8")
    pgdata = tmp_path / "pgdata"; pgdata.mkdir()
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "POSTGRES_USER": "postgres", "DB_ACCOUNT_SECRETS_DIR": str(sec), "PGDATA": str(pgdata)}
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    calls = log.read_text(encoding="utf-8")
    argv_lines = [l for l in calls.splitlines() if l.startswith("ARGV:")]
    assert argv_lines and all(SECRET not in l for l in argv_lines)
    assert "pw=" not in "\n".join(argv_lines)
    assert "\\set pw `head -n1" in calls and "'pw'" in calls      # 암호는 표준 입력의 메타명령으로만 들어간다
    assert SECRET not in r.stdout + r.stderr
