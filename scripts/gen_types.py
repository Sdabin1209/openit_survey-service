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
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
OUT = ROOT / "survey_service" / "types"
if "--out" in sys.argv:      # 구조 검사 5번이 임시 폴더에 다시 생성해 비교한다
    OUT = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])

VALIDATION = '''"""생성 파일(scripts/gen_types.py). 손으로 고치지 않는다. 스키마 검증 한 곳: validate / errors."""
from __future__ import annotations

import collections
import functools
import json
import pathlib
from typing import Any

import jsonschema
from boundary_contracts import format_checker as _bc_format_checker  # 형식(format) 검증기가 없으면 조용히 넘기지 않고 실패한다(계약 3.5.0)
from boundary_contracts import schema_validator as _schema_validator

_DIR = pathlib.Path(__file__).parent / "_schemas"


def draft_validator(sch: dict):
    """문진 스키마·영역 스키마의 검증기 한 곳. 왜 jsonschema.Draft202012Validator 를 직접 만들지 않나: `boundary_contracts.schema_validator` 는 NaN·무한대를 먼저 거부하고(5.0.0, I-JSON) 계약 registry 로 외부 $ref(계약 안정 URI, 문진 스키마)를 풀고 format_checker 를 강제한다.
    registry 없이 만든 검증기는 그 $ref 를 못 풀어 실패한다(조용히 넓어지지 않는다)."""
    return _schema_validator(sch)      # 계약 5.0.1: 계약 registry(안정 URI)·format_checker 강제·비유한 수 거부를 한 번에


@functools.lru_cache(maxsize=None)
def schema(name: str) -> dict:
    return json.loads((_DIR / f"{name}.json").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def _validator(name: str):
    return draft_validator(schema(name))


def _external(node: dict, value: Any):
    """x-contract(boundary-contracts)·x-survey-schema(문진 스키마)가 붙은 자리는 그 정본 검증기로 본다(복사하지 않는다)."""
    if "x-contract" in node:
        from boundary_contracts import validator
        yield from validator(node["x-contract"]).iter_errors(value)
    if "x-survey-schema" in node:
        import survey_schema
        yield from draft_validator(survey_schema.load_schema(node["x-survey-schema"])).iter_errors(value)


def _ok(sub: dict, value: Any, root: Any) -> bool:
    """가지 하나가 값에 구조적으로 맞는가(루트의 $defs를 함께 준다)."""
    return draft_validator({**sub, "$defs": (root or {}).get("$defs", {})}).is_valid(value)


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


_PASSED: "collections.OrderedDict[tuple, Any]" = collections.OrderedDict()      # 이미 통과한 오래 사는 문서(문항 세트, 문항 흐름, 표). 객체가 바뀌지 않는다는 약속(입력 문서는 바꾸지 않는다)에 기대어 같은 객체는 한 번만 본다
_PASSED_MAX = 256


@functools.lru_cache(maxsize=None)
def _relaxed(name: str, static: tuple):
    """static 속성의 자리를 비워 둔 검증기(그 속성은 따로 한 번만 검증한다)."""
    sch = json.loads(json.dumps(schema(name)))
    for k in static:
        sch["properties"][k] = {}
    return draft_validator(sch)


def _fmt(e) -> str:
    return f"{'/'.join(str(p) for p in e.absolute_path) or '(최상위)'}: {e.validator}"      # 왜 e.message 가 아닌가: jsonschema 메시지는 위반한 값 자체를 담는다(환자 발화일 수 있다). 위치와 키워드(스키마가 정한 이름)만 싣는다(INTERFACES I7)


def errors(name: str, value: Any, static: tuple = ()) -> list[str]:
    """위반 목록(경로: 이유). 비어 있으면 통과. 값 자체는 메시지에 넣지 않는다.
    static: 이 입력에서 오래 사는 문서를 담은 속성 이름들(예: 문항 세트). 느려서(문항 세트 한 번에 약 100ms, 측정) 같은 객체는 한 번만 검증한다."""
    sch = schema(name)
    if not static or not isinstance(value, dict):
        out = [_fmt(e) for e in _validator(name).iter_errors(value)]
        out += [_fmt(e) for e in _walk(sch, value, sch)]
        return sorted(set(out))
    out = [_fmt(e) for e in _relaxed(name, tuple(static)).iter_errors(value)]
    for k in static:
        if k not in value:
            continue
        key = (name, k, id(value[k]))
        if key in _PASSED and _PASSED[key] is value[k]:
            continue
        sub = sch["properties"][k]
        defs = sch.get("$defs", {})
        errs = [f"{k}/{_fmt(e)}" for e in draft_validator({**sub, "$defs": defs}).iter_errors(value[k])]
        errs += [f"{k}/{_fmt(e)}" for e in _walk(sub, value[k], {**sch, **sub})]
        if errs:
            out += errs
        else:
            _PASSED[key] = value[k]
            if len(_PASSED) > _PASSED_MAX:
                _PASSED.popitem(last=False)
    return sorted(set(out))


def validate(name: str, value: Any, static: tuple = ()) -> None:
    errs = errors(name, value, static)
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
    (OUT / "contract").mkdir()
    (OUT / "contract/__init__.py").write_text("# Generated contract types. Do not edit.\n")
    for f in files:
        name = f.stem
        names.append(name)
        shutil.copyfile(f, OUT / "_schemas" / f.name)
        source = json.loads(f.read_text())
        delegated = source.get("x-contract", "").startswith("PublicApiRequests#")
        output = OUT / "contract" / f"{name}.py" if delegated else OUT / f"{name}.py"
        with tempfile.TemporaryDirectory() as stage:
            input_path = f
            if delegated:
                materialized = subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/contract_schema_codegen.py"), source["x-contract"]], capture_output=True, text=True, check=True)
                input_path = pathlib.Path(stage) / f.name
                input_path.write_text(materialized.stdout)
            r = subprocess.run([sys.executable, "-m", "datamodel_code_generator", "--input", str(input_path), "--input-file-type", "jsonschema",
                                "--output", str(output), "--output-model-type", "typing.TypedDict", "--target-python-version", "3.10",
                                "--use-standard-collections", "--use-union-operator", "--disable-timestamp", "--class-name", name,
                                "--formatters", "black", "isort"], capture_output=True, text=True, cwd=ROOT)
        if delegated and r.returncode == 0:
            (OUT / f"{name}.py").write_text(f"# Generated alias to the boundary contract.\nfrom .contract.{name} import {name}  # noqa: F401\n")
        if r.returncode != 0:
            print(f"생성 실패 {name}: {r.stderr[-400:]}", file=sys.stderr)
            return 1
    (OUT / "validation.py").write_text(VALIDATION, encoding="utf-8")
    init = ['"""생성 파일(scripts/gen_types.py). 손으로 고치지 않는다."""', "from .validation import SchemaViolation, draft_validator, errors, schema, validate  # noqa: F401", ""]
    init += [f"from .{n} import {n}  # noqa: F401" for n in names]
    init += ["", f"__all__ = ['SchemaViolation', 'errors', 'schema', 'validate', {', '.join(repr(n) for n in names)}]", ""]
    (OUT / "__init__.py").write_text("\n".join(init), encoding="utf-8")
    print(f"types/ 생성: {len(names)}개 타입")
    return 0


if __name__ == "__main__":
    sys.exit(main())
