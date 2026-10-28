"""CR-018 초안 스키마를 예시 문서로 검증한다. 사용: .venv/bin/python docs/change-requests/schemas/verify_boundary_schemas.py
유효 예시는 통과, 거부 예시는 위반이어야 한다. 하나라도 어긋나면 종료 코드 1. 예시 문서는 실제 코드·설정에서 가져왔다(CR-018 본문)."""
import json
import pathlib
import sys

import jsonschema

H = pathlib.Path(__file__).parent
E = H / "examples"
CASES = [  # (스키마, 예시 파일, 통과해야 하는가, 스키마 안의 하위 키)
    ("service-env", "service-env.valid.json", True, None), ("service-env", "service-env.invalid.json", False, None),
    ("readyz-body", "readyz-body.valid.json", True, None), ("readyz-body", "readyz-body.invalid.json", False, None),
    ("purge-output", "purge-output.valid.json", True, None), ("purge-output", "purge-output.valid-session.json", True, None),
    ("purge-output", "purge-output.invalid.json", False, None),
    ("image-labels", "image-labels.valid.json", True, None), ("image-labels", "image-labels.invalid.json", False, None),
    ("question-flow-table-args", "question-flow-table-args.valid.json", True, None),
    ("question-flow-table-args", "question-flow-table-args.invalid.json", False, None),
    ("question-bank-read", "question-bank-read.valid-request.json", True, "request"),
    ("question-bank-read", "question-bank-read.valid-response.json", True, "response"),
    ("question-bank-read", "question-bank-read.invalid-response.json", False, "response"),
]
bad = 0
for name, ex, want_ok, sub in CASES:
    schema = json.loads((H / f"{name}.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    if sub:
        schema = {**schema["properties"][sub], "$schema": schema["$schema"]}
    errs = list(jsonschema.Draft202012Validator(schema).iter_errors(json.loads((E / ex).read_text())))
    ok = not errs
    flag = "OK  " if ok == want_ok else "FAIL"
    bad += ok != want_ok
    print(f"{flag} {ex:48} {'통과' if ok else '거부'}" + ("" if ok else f" ({errs[0].validator}: {'/'.join(map(str, errs[0].absolute_path))})"))
# 거부 규칙을 하나씩 따로 확인한다(유효 예시를 한 군데만 망가뜨린다)
def mutated(name, ex, fn, want_validator):
    global bad
    d = json.loads((E / ex).read_text())
    fn(d)
    errs = list(jsonschema.Draft202012Validator(json.loads((H / f"{name}.schema.json").read_text())).iter_errors(d))
    hit = any(e.validator == want_validator or want_validator in str(e.schema_path) for e in errs)
    bad += not hit
    print(("OK   " if hit else "FAIL ") + f"{name}: {fn.__doc__} -> {'거부' if errs else '통과(잘못)'}")
def typo(d):
    "SURVEY_ 접두 오타 키 하나"; d["SURVEY_TRACE_INGST"] = "http"
def both(d):
    "비밀을 NAME과 NAME_FILE로 동시에"; d["SURVEY_STAFF_API_TOKEN"] = "x"
def nourl(d):
    "http인데 URL 없음"; del d["SURVEY_RELEASE_READER_URL"]
def lbl(d):
    "io.openit.* 모르는 키 하나"; d["io.openit.langgrph"] = "1"
def prob(d):
    "ready=false인데 problems 비어 있음"; d["ready"] = False; d["problems"] = []
mutated("service-env", "service-env.valid.json", typo, "propertyNames")
mutated("service-env", "service-env.valid.json", both, "not")
mutated("service-env", "service-env.valid.json", nourl, "required")
mutated("image-labels", "image-labels.valid.json", lbl, "propertyNames")
mutated("readyz-body", "readyz-body.valid.json", prob, "minItems")
# 문항 세트 본문은 문진 스키마로도 검증한다(question-bank-read 응답의 question_bank)
sys.path.insert(0, str(H.parents[2]))
import survey_schema  # noqa: E402
bank = json.loads((E / "question-bank-read.valid-response.json").read_text())["question_bank"]
jsonschema.Draft202012Validator(survey_schema.load_schema("QuestionBank", 1)).validate(bank)
print("OK   question_bank는 문진 스키마 QuestionBank(정수 버전 1)도 통과")
from boundary_contracts.canonical import sha256_hash  # noqa: E402
resp = json.loads((E / "question-bank-read.valid-response.json").read_text())
assert resp["content_hash"] == sha256_hash(resp["question_bank"]), "content_hash가 정규화 JSON 해시와 다르다"
print("OK   content_hash == sha256_hash(question_bank) (boundary_contracts.canonical)")
print(f"검증 {len(CASES)}건, 어긋남 {bad}건")
sys.exit(1 if bad else 0)
