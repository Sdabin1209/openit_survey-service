#!/usr/bin/env python3
"""schemas/ 에서 survey_service/types/ 를 생성한다(DESIGN.md 3절, 6절 5번). types/ 는 손으로 고치지 않는다.

도구: datamodel-code-generator (requirements-tools.txt에 버전 고정). 실행: scripts/gen-types.sh
산출: types/<타입>.py (TypedDict), types/_schemas/<타입>.json (스키마 사본: 실행 때 검증에 쓴다), types/__init__.py, types/validation.py.
왜 스키마 사본을 types/에 넣나: 이미지·wheel에는 패키지 안의 파일만 들어간다. 사본도 생성물이라 schemas/ 와 어긋나면 구조 검사 5번이 잡는다.
"""
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
OUT = ROOT / "survey_service" / "types"
if "--out" in sys.argv:      # 구조 검사 5번이 임시 폴더에 다시 생성해 비교한다
    OUT = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])

VALIDATION = '''"""생성 파일(scripts/gen_types.py). 손으로 고치지 않는다. 스키마 검증 한 곳: validate / errors."""
from __future__ import annotations

import functools
import json
import pathlib
from typing import Any

import jsonschema

_DIR = pathlib.Path(__file__).parent / "_schemas"


@functools.lru_cache(maxsize=None)
def schema(name: str) -> dict:
    return json.loads((_DIR / f"{name}.json").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def _validator(name: str):
    return jsonschema.Draft202012Validator(schema(name))


def _external(node: dict, value: Any):
    """x-contract(boundary-contracts)·x-survey-schema(문진 스키마)가 붙은 자리는 그 정본 검증기로 본다(복사하지 않는다)."""
    if "x-contract" in node:
        from boundary_contracts import validator
        yield from validator(node["x-contract"]).iter_errors(value)
    if "x-survey-schema" in node:
        import survey_schema
        yield from jsonschema.Draft202012Validator(survey_schema.load_schema(node["x-survey-schema"])).iter_errors(value)


def _ok(sub: dict, value: Any, root: Any) -> bool:
    """가지 하나가 값에 구조적으로 맞는가(루트의 $defs를 함께 준다)."""
    return jsonschema.Draft202012Validator({**sub, "$defs": (root or {}).get("$defs", {})}).is_valid(value)


def _walk(node: Any, value: Any, root: Any = None):
    """properties, items, allOf, 구조가 맞는 anyOf·oneOf 가지, 성립한 if의 then, 같은 파일 안 $ref를 따라 내려가며 외부 정본 검증을 건다."""
    if not isinstance(node, dict):
        return
    root = root if root is not None else node
    yield from _external(node, value)
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target: Any = root
        for part in ref[2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        yield from _walk(target, value, root)
    for sub in node.get("allOf") or []:
        yield from _walk(sub, value, root)
    for key in ("anyOf", "oneOf"):
        for sub in node.get(key) or []:
            if isinstance(sub, dict) and _ok(sub, value, root):
                yield from _walk(sub, value, root)
    if isinstance(node.get("if"), dict) and isinstance(node.get("then"), dict) and _ok(node["if"], value, root):
        yield from _walk(node["then"], value, root)
    if isinstance(value, dict):
        for k, sub in (node.get("properties") or {}).items():
            if k in value:
                yield from _walk(sub, value[k], root)
    if isinstance(value, list) and isinstance(node.get("items"), dict):
        for v in value:
            yield from _walk(node["items"], v, root)


def errors(name: str, value: Any) -> list[str]:
    """위반 목록(경로: 이유). 비어 있으면 통과. 값 자체는 메시지에 넣지 않는다."""
    out = [f"{'/'.join(str(p) for p in e.absolute_path) or '(최상위)'}: {e.message[:200]}" for e in _validator(name).iter_errors(value)]
    out += [f"{'/'.join(str(p) for p in e.absolute_path) or '(최상위)'}: {e.message[:200]}" for e in _walk(schema(name), value, schema(name))]
    return sorted(set(out))


def validate(name: str, value: Any) -> None:
    errs = errors(name, value)
    if errs:
        raise SchemaViolation(name, errs)


class SchemaViolation(ValueError):
    def __init__(self, name: str, errs: list[str]) -> None:
        super().__init__(f"{name}: " + "; ".join(errs[:3]) + (f" (위반 {len(errs)}건)" if len(errs) > 3 else ""))
        self.type_name, self.errors = name, errs
'''


def main() -> int:
    files = sorted(p for p in SCHEMAS.glob("*.json"))
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "_schemas").mkdir(parents=True)
    names = []
    for f in files:
        name = f.stem
        names.append(name)
        shutil.copyfile(f, OUT / "_schemas" / f.name)
        r = subprocess.run([sys.executable, "-m", "datamodel_code_generator", "--input", str(f), "--input-file-type", "jsonschema",
                            "--output", str(OUT / f"{name}.py"), "--output-model-type", "typing.TypedDict", "--target-python-version", "3.10",
                            "--use-standard-collections", "--use-union-operator", "--disable-timestamp", "--class-name", name,
                            "--formatters", "black", "isort"], capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            print(f"생성 실패 {name}: {r.stderr[-400:]}", file=sys.stderr)
            return 1
    (OUT / "validation.py").write_text(VALIDATION, encoding="utf-8")
    init = ['"""생성 파일(scripts/gen_types.py). 손으로 고치지 않는다."""', "from .validation import SchemaViolation, errors, schema, validate  # noqa: F401", ""]
    init += [f"from .{n} import {n}  # noqa: F401" for n in names]
    init += ["", f"__all__ = ['SchemaViolation', 'errors', 'schema', 'validate', {', '.join(repr(n) for n in names)}]", ""]
    (OUT / "__init__.py").write_text("\n".join(init), encoding="utf-8")
    print(f"types/ 생성: {len(names)}개 타입")
    return 0


if __name__ == "__main__":
    sys.exit(main())
