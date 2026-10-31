"""구조 검사(DESIGN.md 6절, D117). 헤드의 head/scripts/structure-check.py와 같은 기준을 더 엄격하게(상대 import까지) 본다.

1 디렉터리  2 컴포넌트끼리 import 금지  3 HTTP는 link_* 만  4 스키마 예시  5 types 재생성 일치  6 코드 파일의 자리
"""
import ast
import json
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "survey_service"
COMPONENTS = PKG / "components"
# 컴포넌트가 import해도 되는 컴포넌트 밖 모듈: 설정·마이그레이션·생성 타입뿐이다. wiring/은 import하지 않는다(DESIGN 6절, L1 해소)
ALLOWED_OUTSIDE = {"settings", "migrate", "types", "config"}
HTTP_MODULES = {"boundary_contracts.clients", "urllib.request", "http.client", "http.server", "requests", "waitress", "wsgiref", "pywebpush", "aiohttp", "httpx"}
ENTRY = re.compile(r"(^|/)(__main__|main|app|server|config|settings|migrate|cli|index)\.py$|(^|/)migrations?/")


def _py(root):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path):
    """(절대 모듈 이름, 가져오는 이름들) 목록. 상대 import도 survey_service 기준 절대 이름으로 바꾼다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [(a.name, []) for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = pkg.split(".")[: len(pkg.split(".")) - (n.level - 1)]
                base = ".".join(parts + ([base] if base else []))
            out.append((base, [a.name for a in n.names]))
    return out


def test_1_required_directories_exist():
    for d in (ROOT / "schemas", PKG / "types", COMPONENTS, PKG / "wiring"):
        assert d.is_dir() and any(d.iterdir()), f"{d.relative_to(ROOT)} 없음 또는 비어 있음"


def test_2_components_do_not_import_each_other():
    comps = {p.stem for p in _py(COMPONENTS) if p.stem != "__init__"}
    bad = []
    for p in _py(COMPONENTS):
        for mod, names in _imports(p):
            rel = mod[len("survey_service."):] if mod.startswith("survey_service.") else mod
            if rel == "survey_service.components" or rel == "components":
                bad += [f"{p.name}: components 패키지에서 {n}" for n in names if n in comps and n != p.stem]
            if rel.startswith("components."):
                bad.append(f"{p.name}: {rel}")
            if rel == "wiring" or rel.startswith("wiring."):
                bad.append(f"{p.name}: 컴포넌트가 wiring을 import한다 ({rel})")
            if rel in ("orchestrator", "server"):
                bad.append(f"{p.name}: {rel}")
    assert not bad, bad


def test_3_http_only_in_link_components_and_entry():
    bad = []
    for p in _py(PKG):
        if p.name.startswith("link_") or ENTRY.search(p.relative_to(ROOT).as_posix()):
            continue
        for mod, _ in _imports(p):
            if any(mod == m or mod.startswith(m + ".") for m in HTTP_MODULES):
                bad.append(f"{p.relative_to(ROOT)}: {mod}")
    assert not bad, bad


def test_4_every_schema_has_valid_and_invalid_examples_with_right_results():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_schemas.py")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]
    assert list((ROOT / "schemas").glob("*.json")), "schemas/ 가 비어 있다"


def test_5_types_equal_regenerated_output(tmp_path):
    tools = ROOT / ".venv-tools" / "bin" / "python"
    assert tools.exists(), "도구 환경이 없다: bash scripts/gen-types.sh 를 먼저 실행한다(README 4절)"
    out = tmp_path / "types"
    r = subprocess.run([str(tools), str(ROOT / "scripts" / "gen_types.py"), "--out", str(out)], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-800:]
    have = {p.relative_to(PKG / "types"): p.read_bytes() for p in (PKG / "types").rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    want = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert have == want, sorted(set(have) ^ set(want)) or [str(k) for k in have if have[k] != want[k]][:5]


def test_6_every_code_file_has_a_place():
    bad = []
    for p in _py(PKG):
        rel = p.relative_to(PKG).as_posix()
        if p.name == "__init__.py":
            continue
        if rel.startswith(("components/", "wiring/", "types/")) or ENTRY.search(rel):
            continue
        bad.append(rel)
    assert not bad, bad


def test_components_have_no_stray_modules_without_a_row_in_components_md():
    doc = (ROOT / "docs" / "components.md").read_text(encoding="utf-8")
    missing = [p.stem for p in _py(COMPONENTS) if p.stem != "__init__" and f"components/{p.stem}.py" not in doc]
    assert not missing, f"docs/components.md 표에 없는 컴포넌트: {missing}"


def test_components_never_read_the_time_module_directly():
    """ADR-0050: 컴포넌트는 `time`을 import하지 않는다. 시계는 인자로 받고, 기본값은 `survey_service/config.py`(조립 기본값)에서 온다."""
    bad = []
    for p in _py(COMPONENTS):
        for mod, names in _imports(p):
            if mod == "time" or mod.startswith("time."):
                bad.append(f"{p.name}: import {mod}")
    assert not bad, bad
