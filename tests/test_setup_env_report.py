"""scripts/setup-env.sh의 마지막 출력 줄 = 계약 SetupEnvReport(boundary-contracts 2.6.0, ADR-0039).
실제 .venv를 건드리지 않으려고(시험이 도는 환경을 지우고 다시 깔면 안 된다) 최소 리포 사본(핀 없음, 패키지 하나)에서 스크립트를 돌린다."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from boundary_contracts import validator

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def mini_repo(tmp_path_factory):
    d = tmp_path_factory.mktemp("mini")
    (d / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/setup-env.sh", d / "scripts/setup-env.sh")
    (d / "requirements.txt").write_text("# 핀 없음(시험용 최소 리포)\n")
    (d / "requirements.lock").write_text("# 잠금 없음\n")
    (d / "pyproject.toml").write_text('[project]\nname = "mini-area"\nversion = "0.0.1"\n[tool.setuptools]\npackages = []\n')
    return d


def run(repo):
    return subprocess.run(["bash", "scripts/setup-env.sh"], cwd=repo, capture_output=True, text=True, timeout=HANG_GUARD_SECONDS,
                          env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"})


def test_last_stdout_line_is_a_valid_setup_env_report_and_progress_goes_to_stderr(mini_repo):
    r = run(mini_repo)
    assert r.returncode == 0, r.stderr[-600:]
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 1                                            # 표준 출력에는 보고뿐이다(진행 기록은 표준 오류)
    rep = json.loads(lines[-1])
    assert list(validator("SetupEnvReport").iter_errors(rep)) == []
    assert rep["area"] == "survey-service" and rep["status"] == "ok" and rep["exit_code"] == 0
    assert rep["environment"] == {"kind": "python-venv", "path": ".venv", "test_command": [".venv/bin/python", "-m", "pytest"]}
    assert "[setup-env]" in r.stderr
    assert (mini_repo / rep["environment"]["path"] / "bin/python").exists()          # 선언한 경로가 실제 환경이다


def test_second_run_is_idempotent_and_still_reports_last(mini_repo):
    r = run(mini_repo)
    assert r.returncode == 0 and json.loads(r.stdout.strip().splitlines()[-1])["status"] == "ok"


def test_failure_still_reports_status_fail_with_the_exit_code(tmp_path):
    d = tmp_path / "bad"
    (d / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/setup-env.sh", d / "scripts/setup-env.sh")          # requirements.txt 없음 -> 실패
    r = run(d)
    assert r.returncode != 0
    rep = json.loads(r.stdout.strip().splitlines()[-1])
    assert list(validator("SetupEnvReport").iter_errors(rep)) == [] and rep["status"] == "fail" and rep["exit_code"] == r.returncode


def test_declared_test_command_runs_the_area_tests_from_the_repo_root():
    """선언한 argv가 리포 루트에서 실행되는지: 시험을 모으기만 한다(전체 실행은 이 시험을 다시 부르므로 하지 않는다. 전체 실행은 보고에서 따로 한다)."""
    venv = ROOT / ".venv/bin/python"
    if not venv.exists():
        pytest.skip(".venv가 없다(setup-env.sh를 먼저 실행)")
    r = subprocess.run([".venv/bin/python", "-m", "pytest", "--collect-only", "-p", "no:cacheprovider", "tests/test_version_pins_and_labels.py"],
                       cwd=ROOT, capture_output=True, text=True, timeout=HANG_GUARD_SECONDS)
    assert r.returncode == 0, r.stdout[-300:] + r.stderr[-300:]
