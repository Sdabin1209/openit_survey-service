#!/usr/bin/env python3
"""schemas/ 점검: 모든 스키마가 draft 2020-12이고, 통과 예시(<타입>.valid*.json)는 통과, 거부 예시(<타입>.invalid*.json)는 거부되는지 본다.
사용: .venv/bin/python scripts/check_schemas.py [타입 ...]   종료 코드 0이면 전부 맞다. 스키마 검증은 types/validation.py와 같은 규칙(x-contract, x-survey-schema)."""
import json
import pathlib
import sys

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCHEMAS = ROOT / "schemas"


def load_validation():
    import importlib.util
    src = (ROOT / "scripts" / "gen_types.py").read_text(encoding="utf-8")
    ns: dict = {}
    code = src[src.index("VALIDATION = '''") + len("VALIDATION = '''"):src.index("'''\n\n\ndef main")]
    spec = importlib.util.spec_from_loader("ss_validation", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__["__file__"] = str(ROOT / "survey_service" / "types" / "validation.py")
    exec(compile(code, "validation", "exec"), mod.__dict__)
    return mod


def main(argv) -> int:
    v = load_validation()
    v._DIR = SCHEMAS
    v.schema.cache_clear(); v._validator.cache_clear()
    bad = 0
    names = argv or sorted(p.stem for p in SCHEMAS.glob("*.json"))
    for n in names:
        try:
            s = json.loads((SCHEMAS / f"{n}.json").read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(s)
        except Exception as e:
            print(f"[스키마 오류] {n}: {type(e).__name__} {str(e)[:150]}"); bad += 1; continue
        ex = sorted((SCHEMAS / "examples").glob(f"{n}.*.json"))
        valid = [p for p in ex if p.name.startswith(f"{n}.valid")]
        invalid = [p for p in ex if p.name.startswith(f"{n}.invalid")]
        if not valid or not invalid:
            print(f"[예시 부족] {n}: valid {len(valid)} invalid {len(invalid)}"); bad += 1
        for p in valid:
            errs = v.errors(n, json.loads(p.read_text(encoding="utf-8")))
            if errs:
                print(f"[통과해야 함] {p.name}: {errs[0]}"); bad += 1
        for p in invalid:
            if not v.errors(n, json.loads(p.read_text(encoding="utf-8"))):
                print(f"[거부해야 함] {p.name}"); bad += 1
    print(f"스키마 {len(names)}개 점검, 문제 {bad}건")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
