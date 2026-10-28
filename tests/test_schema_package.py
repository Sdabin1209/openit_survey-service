"""문진 스키마 패키지(schema/)가 wheel로 빌드되고, 설치본에서 버전별로 스키마를 얻을 수 있는지 확인한다."""
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema"


def _build(src: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "--no-build-isolation", "-w", str(out), str(src)],
                   check=True, capture_output=True)
    return next(out.glob("*.whl"))


def _run_isolated(extracted: Path, code: str) -> str:
    pre = f"import sys; sys.path.insert(0, {str(extracted)!r})\n"
    r = subprocess.run([sys.executable, "-I", "-c", pre + code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    src = tmp_path_factory.mktemp("src") / "schema"
    shutil.copytree(SCHEMA, src, ignore=shutil.ignore_patterns("*.egg-info", "build", "__pycache__"))
    return _build(src, tmp_path_factory.mktemp("whl"))


def test_wheel_contains_versioned_schemas(wheel):
    names = zipfile.ZipFile(wheel).namelist()
    originals = sorted(p.name for p in (SCHEMA / "schemas").glob("*.json"))
    assert originals
    for n in originals:
        assert f"survey_schema/v1/schemas/{n}" in names
    assert "survey_schema/v1/CONTRACT_VERSION" in names


def test_installed_loader(wheel, tmp_path):
    zipfile.ZipFile(wheel).extractall(tmp_path)
    out = _run_isolated(tmp_path, (
        "import json, survey_schema as s;"
        "print(json.dumps({'v': s.available_versions(), 'cur': s.current_version(),"
        "'dir': str(s.schema_dir(1)), 'id': s.load_schema('TurnTrace', 1)['$id'], 'n': len(s.list_schemas(1)),"
        "'cv': (s.schema_dir(1).parent / 'CONTRACT_VERSION').read_text().strip()}))"))
    d = json.loads(out)
    assert d["v"] == [1] and d["cur"] == 1 and d["cv"] == "1"
    assert d["id"] == "standalone/TurnTrace.json" and d["n"] == 16
    assert d["dir"].startswith(str(tmp_path))


def test_unknown_version_and_name(wheel, tmp_path):
    zipfile.ZipFile(wheel).extractall(tmp_path)
    out = _run_isolated(tmp_path, (
        "import survey_schema as s\n"
        "for f in (lambda: s.schema_dir(2), lambda: s.load_schema('NoSuch')):\n"
        "    try: f()\n    except KeyError: print('KeyError')\n"))
    assert out.splitlines() == ["KeyError", "KeyError"]


def test_multiple_integer_versions(tmp_path):
    """여러 정수 버전을 동시에 지원하는 소비자: schema/v2/가 있으면 같은 wheel에 v2도 들어간다."""
    src = tmp_path / "schema"
    shutil.copytree(SCHEMA, src, ignore=shutil.ignore_patterns("*.egg-info", "build", "__pycache__"))
    (src / "v2" / "schemas").mkdir(parents=True)
    (src / "v2" / "CONTRACT_VERSION").write_text("2\n")
    (src / "v2" / "schemas" / "TurnTrace.json").write_text(json.dumps({"$id": "standalone/TurnTrace.json", "title": "v2"}))
    wh = _build(src, tmp_path / "out")
    ex = tmp_path / "ex"
    zipfile.ZipFile(wh).extractall(ex)
    out = _run_isolated(ex, ("import survey_schema as s;"
                             "print(s.available_versions(), s.current_version(), s.load_schema('TurnTrace', 2)['title'],"
                             "'$defs' in s.load_schema('TurnTrace', 1))"))
    assert out == "[1, 2] 2 v2 True"


def test_source_tree_loader_matches_files():
    import survey_schema as s
    assert s.schema_dir(1) == SCHEMA / "schemas"
    assert s.load_schema("QuestionBank", 1) == json.loads((SCHEMA / "schemas" / "QuestionBank.json").read_text(encoding="utf-8"))
    assert (SCHEMA / "CONTRACT_VERSION").read_text().strip() == "1"


def _pkg_version() -> tuple:
    import re
    m = re.search(r'^version = "(\d+)\.(\d+)\.(\d+)"', (ROOT / "schema/pyproject.toml").read_text(), re.M)
    return tuple(int(x) for x in m.groups())


def test_package_version_major_is_the_contract_integer_version():
    """태그 survey-schema-vMAJOR.MINOR.PATCH에서 MAJOR는 정수 CONTRACT_VERSION이다(D110). 비호환 변경은 정수를 올리고 MAJOR가 같이 오른다."""
    assert _pkg_version()[0] == int((ROOT / "schema/CONTRACT_VERSION").read_text().strip())


def test_package_version_is_not_older_than_the_latest_schema_tag():
    """스키마를 바꾸고 패키지 버전을 올리지 않으면 설치본 표기가 태그보다 낡다(1.0.0 표기로 1.1.0 내용이 나갔던 일). git 태그가 없으면 건너뛴다."""
    import re
    import subprocess
    r = subprocess.run(["git", "tag", "--list", "survey-schema-v*"], cwd=ROOT, capture_output=True, text=True)
    tags = [tuple(int(x) for x in m.groups()) for t in r.stdout.split() if (m := re.fullmatch(r"survey-schema-v(\d+)\.(\d+)\.(\d+)", t))]
    if r.returncode != 0 or not tags:
        pytest.skip("survey-schema-v* 태그를 읽을 수 없다")
    assert _pkg_version() >= max(tags)


def test_package_version_equals_the_tag_when_head_is_tagged():
    """HEAD에 survey-schema-vX.Y.Z 태그가 붙어 있으면 패키지 버전은 그 X.Y.Z와 같다(설치본 표기와 태그가 어긋나지 않게, D110). 태그 전에는 건너뛴다."""
    import re
    import subprocess
    r = subprocess.run(["git", "tag", "--points-at", "HEAD", "--list", "survey-schema-v*"], cwd=ROOT, capture_output=True, text=True)
    here = [tuple(int(x) for x in m.groups()) for t in r.stdout.split() if (m := re.fullmatch(r"survey-schema-v(\d+)\.(\d+)\.(\d+)", t))]
    if r.returncode != 0 or not here:
        pytest.skip("HEAD에 survey-schema-v* 태그가 없다")
    assert _pkg_version() == here[0]
