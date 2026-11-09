"""생성 파일(scripts/gen_types.py). 손으로 고치지 않는다. 스키마 검증 한 곳: validate / errors."""
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


_OK_CACHE: dict = {}      # (id(가지), id($defs)) -> (가지, $defs, 검증기). 가지·$defs 를 함께 쥐어 id 재사용을 막는다
_NO_DEFS: dict = {}


def _ok(sub: dict, value: Any, root: Any) -> bool:
    """가지 하나가 값에 구조적으로 맞는가(루트의 $defs를 함께 준다).
    왜 검증기를 캐시하나: 호출마다 만들면 가지당 약 30ms(측정, 문진 AlertWrite 한 번 검증에 0.5초)라 /readyz 보조 수치가 STATS_GRACE_S(0.2초)를 넘겨 항상 stale 이 된다(codex C2 검토, 2026-11-09)."""
    defs = (root or {}).get("$defs", _NO_DEFS)
    hit = _OK_CACHE.get((id(sub), id(defs)))
    if hit is None or hit[0] is not sub or hit[1] is not defs:
        if len(_OK_CACHE) >= 4096:
            _OK_CACHE.clear()
        hit = (sub, defs, draft_validator({**sub, "$defs": defs}))
        _OK_CACHE[(id(sub), id(defs))] = hit
    return hit[2].is_valid(value)


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
