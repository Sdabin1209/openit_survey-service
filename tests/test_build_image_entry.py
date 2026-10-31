"""영역 빌드 진입점 scripts/build-image.sh (계약 boundary-contracts 2.5.0, BuildImageRequest/BuildImageResult, ADR-0037).
요청 검증(모르는 키·필요한 키 없음은 실패)은 docker 없이 시험하고, 실제 빌드는 도커가 있을 때 한 번 한다(이미지 이름에 리포 이름, 끝나면 지운다)."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from boundary_contracts import validator

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv/bin/python"
pytestmark = pytest.mark.skipif(not VENV_PY.exists(), reason=".venv가 없다(bash scripts/setup-env.sh를 먼저 실행: 진입점은 이 영역의 .venv로 요청을 검증한다)")
SCRIPT = ROOT / "scripts/build-image.sh"
TAG = "openit-survey-service-test:entry-{}"


def request(tmp_path, **override):
    req = {"source_dir": str(ROOT), "revision": "abc1234", "versions": {"boundary": "v3.1.1", "model_runtime": "v0.11.7"},
           "dependency_sources": {"boundary": str(tmp_path), "model_runtime": str(tmp_path)}, "image_tag": TAG.format("x")}
    req.update(override)
    f = tmp_path / "req.json"
    f.write_text(json.dumps(req), encoding="utf-8")
    return f


def run(f, **env):
    return subprocess.run(["bash", str(SCRIPT), str(f)], capture_output=True, text=True, cwd=ROOT,
                          env={**{k: v for k, v in os.environ.items() if k != "PYTHON"}, "DOCKER": "false", **env})


def test_request_with_an_unknown_key_is_rejected(tmp_path):
    r = run(request(tmp_path, extra_key=1))
    assert r.returncode != 0 and "BuildImageRequest" in r.stderr


def test_missing_keys_the_area_uses_are_a_failure_not_a_default(tmp_path):
    r = run(request(tmp_path, versions={"boundary": "v3.1.1"}))                                  # model_runtime 없음
    assert r.returncode != 0 and "versions.model_runtime" in r.stderr
    r = run(request(tmp_path, dependency_sources={"boundary": str(tmp_path)}))                  # model_runtime 소스 없음
    assert r.returncode != 0 and "dependency_sources.model_runtime" in r.stderr


def test_unknown_variant_and_wrong_source_dir_are_rejected(tmp_path):
    assert run(request(tmp_path, variant="gpu")).returncode != 0
    assert run(request(tmp_path, source_dir=str(tmp_path))).returncode != 0


def test_turn_engine_is_no_longer_a_request_key(tmp_path):
    """계약 3.0.0: BuildImageRequest.versions.turn_engine은 없어졌다(요청 검증이 거부)."""
    r = run(request(tmp_path, versions={"boundary": "v3.1.1", "model_runtime": "v0.11.7", "turn_engine": "v0.4.3"}))
    assert r.returncode != 0 and "BuildImageRequest" in r.stderr


def _tree(path, name, version):
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "{version}"\n')
    return str(path)


def test_label_honesty_requested_version_that_differs_from_the_tree_fails_without_building(tmp_path):
    """U50: 라벨은 실제로 설치할 소스 트리의 버전에서 찍는다. 요청 버전과 다르면 이미지를 만들지 않고 분명한 메시지로 실패한다(docker를 부르지 않는다)."""
    bc, mr = _tree(tmp_path / "bc", "boundary-contracts", "9.9.9"), _tree(tmp_path / "mr", "model-runtime", "0.11.7")
    r = run(request(tmp_path, dependency_sources={"boundary": bc, "model_runtime": mr}))                 # 요청은 boundary v3.1.1
    assert r.returncode != 0 and "요청 versions.boundary(v3.1.1)" in r.stderr and "실제 버전(v9.9.9)" in r.stderr and "라벨이 거짓" in r.stderr
    bc, mr = _tree(tmp_path / "bc2", "boundary-contracts", "3.1.1"), _tree(tmp_path / "mr2", "model-runtime", "0.10.0")
    r = run(request(tmp_path, dependency_sources={"boundary": bc, "model_runtime": mr}))
    assert r.returncode != 0 and "요청 versions.model_runtime(v0.11.7)" in r.stderr and "실제 버전(v0.10.0)" in r.stderr


def test_label_honesty_requested_survey_schema_version_that_differs_from_the_repo_schema_fails(tmp_path):
    """U50 구멍: 이 이미지가 실제로 쓰는 문진 스키마는 리포의 schema/다. 요청 versions.survey_schema가 그 버전과 다르면 라벨을 찍지 않고 실패한다."""
    actual = "v" + re.search(r'^version = "(.*)"', (ROOT / "schema/pyproject.toml").read_text(), re.M).group(1)
    bc, mr = _tree(tmp_path / "bc", "boundary-contracts", "3.1.1"), _tree(tmp_path / "mr", "model-runtime", "0.11.7")
    r = run(request(tmp_path, versions={"boundary": "v3.1.1", "model_runtime": "v0.11.7", "survey_schema": "v1.3.0"}, dependency_sources={"boundary": bc, "model_runtime": mr}))
    assert r.returncode != 0 and "요청 versions.survey_schema(v1.3.0)" in r.stderr and f"실제 버전({actual})" in r.stderr and "라벨이 거짓" in r.stderr
    r = run(request(tmp_path, versions={"boundary": "v3.1.1", "model_runtime": "v0.11.7", "survey_schema": actual}, dependency_sources={"boundary": bc, "model_runtime": mr}))
    assert "라벨이 거짓" not in r.stderr and "build-image: openit-survey-service-test" in r.stderr          # 같으면 통과해 docker 단계(false)까지 간다


def test_label_honesty_a_tree_of_another_package_is_not_accepted(tmp_path):
    bc, mr = _tree(tmp_path / "bc", "something-else", "3.1.1"), _tree(tmp_path / "mr", "model-runtime", "0.11.7")
    r = run(request(tmp_path, dependency_sources={"boundary": bc, "model_runtime": mr}))
    assert r.returncode != 0 and "boundary-contracts 의 소스 트리가 아니거나" in r.stderr


def test_label_honesty_matching_versions_pass_the_check_and_reach_docker(tmp_path):
    bc, mr = _tree(tmp_path / "bc", "boundary-contracts", "3.1.1"), _tree(tmp_path / "mr", "model-runtime", "0.11.7")
    r = run(request(tmp_path, dependency_sources={"boundary": bc, "model_runtime": mr}))                  # DOCKER=false라 docker 단계에서 멈춘다
    assert "라벨이 거짓" not in r.stderr and "build-image: openit-survey-service-test" in r.stderr


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
        r = subprocess.run(["bash", str(SCRIPT), req.stdout.strip()], capture_output=True, text=True, cwd=ROOT, env={k: v for k, v in os.environ.items() if k != "PYTHON"})       # PYTHON 없이: 진입점이 영역의 .venv를 스스로 고른다
        assert r.returncode == 0, r.stderr[-800:]
        last = r.stdout.strip().splitlines()[-1]
        result = json.loads(last)
        assert list(validator("BuildImageResult").iter_errors(result)) == []
        want = json.loads(Path(req.stdout.strip()).read_text())
        assert result["image"] == tag and result["image_id"].startswith("sha256:")
        labels = result["labels"]
        assert list(validator("ImageLabels").iter_errors(labels)) == []
        inst = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python", tag, "-c", "import importlib.metadata as m; print(m.version('boundary-contracts'), m.version('model-runtime'))"],
                              capture_output=True, text=True).stdout.split()
        assert labels["io.openit.contracts.boundary"] == "v" + inst[0] and labels["io.openit.model-runtime"] == "v" + inst[1]         # 라벨 = 실제 설치 버전(U50)
        assert labels["io.openit.service"] == "survey-service" and labels["io.openit.revision"] == want["revision"]
        assert labels["io.openit.contracts.boundary"] == want["versions"]["boundary"] and labels["io.openit.model-runtime"] == want["versions"]["model_runtime"]
        major = re.search(r'^version = "(\d+)\.', (ROOT / "schema/pyproject.toml").read_text(), re.M).group(1)
        assert labels["io.openit.contracts.survey-schema"] == major           # 라벨 = 이 이미지가 쓰는 문진 스키마(리포의 schema/)의 주 버전. 요청 versions.survey_schema와 같아야 빌드된다
        assert want["versions"]["survey_schema"] == "v" + (ROOT / "schema/pyproject.toml").read_text().split('version = "')[1].split('"')[0]
        assert "io.openit.turn-engine" not in labels
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
