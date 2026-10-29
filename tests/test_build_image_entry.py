"""영역 빌드 진입점 scripts/build-image.sh (계약 boundary-contracts 2.5.0, BuildImageRequest/BuildImageResult, ADR-0037).
요청 검증(모르는 키·필요한 키 없음은 실패)은 docker 없이 시험하고, 실제 빌드는 도커가 있을 때 한 번 한다(이미지 이름에 리포 이름, 끝나면 지운다)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from boundary_contracts import validator

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build-image.sh"
TAG = "openit-survey-service-test:entry-{}"


def request(tmp_path, **override):
    req = {"source_dir": str(ROOT), "revision": "abc1234", "versions": {"boundary": "v2.5.1", "model_runtime": "v0.10.0"},
           "dependency_sources": {"boundary": str(tmp_path), "model_runtime": str(tmp_path)}, "image_tag": TAG.format("x")}
    req.update(override)
    f = tmp_path / "req.json"
    f.write_text(json.dumps(req), encoding="utf-8")
    return f


def run(f, **env):
    return subprocess.run(["bash", str(SCRIPT), str(f)], capture_output=True, text=True, cwd=ROOT,
                          env={**os.environ, "PYTHON": sys.executable, "DOCKER": "false", **env})


def test_request_with_an_unknown_key_is_rejected(tmp_path):
    r = run(request(tmp_path, extra_key=1))
    assert r.returncode != 0 and "BuildImageRequest" in r.stderr


def test_missing_keys_the_area_uses_are_a_failure_not_a_default(tmp_path):
    r = run(request(tmp_path, versions={"boundary": "v2.5.1"}))                                  # model_runtime 없음
    assert r.returncode != 0 and "versions.model_runtime" in r.stderr
    r = run(request(tmp_path, dependency_sources={"boundary": str(tmp_path)}))                  # model_runtime 소스 없음
    assert r.returncode != 0 and "dependency_sources.model_runtime" in r.stderr


def test_unknown_variant_and_wrong_source_dir_are_rejected(tmp_path):
    assert run(request(tmp_path, variant="gpu")).returncode != 0
    assert run(request(tmp_path, source_dir=str(tmp_path))).returncode != 0


def test_unused_turn_engine_version_is_accepted_and_ignored(tmp_path):
    """계약: 폐기된 turn_engine은 받기만 하고 쓰지 않는다. 요청 검증을 통과해 docker 단계(여기서는 false)에서 멈춘다."""
    r = run(request(tmp_path, versions={"boundary": "v2.5.1", "model_runtime": "v0.10.0", "turn_engine": "v0.4.3"}))
    assert "BuildImageRequest를 어긴" not in r.stderr and "쓰는 키가 요청에 없다" not in r.stderr


@pytest.mark.skipif(not shutil.which("docker"), reason="docker 없음")
def test_real_build_prints_a_valid_result_as_the_last_line_with_the_requested_labels(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    tag = TAG.format(os.getpid())
    try:
        req = subprocess.run(["bash", str(ROOT / "scripts/dev-request.sh"), str(work), tag], capture_output=True, text=True, cwd=ROOT,
                             env={**os.environ, "REPO_BASE": os.environ.get("REPO_BASE", "git@github.com:Sdabin1209")})
        if req.returncode != 0:
            pytest.skip("이웃 소스를 받지 못했다(REPO_BASE 또는 네트워크): " + req.stderr[-200:])
        r = subprocess.run(["bash", str(SCRIPT), req.stdout.strip()], capture_output=True, text=True, cwd=ROOT, env={**os.environ, "PYTHON": sys.executable})
        assert r.returncode == 0, r.stderr[-800:]
        last = r.stdout.strip().splitlines()[-1]
        result = json.loads(last)
        assert list(validator("BuildImageResult").iter_errors(result)) == []
        want = json.loads(Path(req.stdout.strip()).read_text())
        assert result["image"] == tag and result["image_id"].startswith("sha256:")
        labels = result["labels"]
        assert list(validator("ImageLabels").iter_errors(labels)) == []
        assert labels["io.openit.service"] == "survey-service" and labels["io.openit.revision"] == want["revision"]
        assert labels["io.openit.contracts.boundary"] == want["versions"]["boundary"] and labels["io.openit.model-runtime"] == want["versions"]["model_runtime"]
        assert labels["io.openit.contracts.survey-schema"] == (ROOT / "schema/CONTRACT_VERSION").read_text().strip() and labels["io.openit.turn-engine"] == "n/a"
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
