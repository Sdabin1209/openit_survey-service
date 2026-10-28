"""계약 승격용 스키마(contract-ready)를 검증한다. 사용(리포 루트에서): .venv/bin/python docs/change-requests/contract-ready/verify_contract_ready.py
1) 형식: 파일 이름·$id·title·$schema 규칙, 스키마 자체 검증, MANIFEST와 파일의 일치
2) 예시: examples/valid는 통과, examples/invalid는 거부(MANIFEST의 def가 있으면 그 $defs로)
3) 실제 코드: 환경 변수 이름 정적 대조 + Settings.from_env, 실제 App.readiness() 출력, 실제 `purge` 출력, 문항 세트 해시
하나라도 어긋나면 종료 코드 1."""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import jsonschema

H = pathlib.Path(__file__).resolve().parent
ROOT = H.parents[2]
sys.path.insert(0, str(ROOT))
bad = 0
n_ok = n_rej = 0


def say(ok, msg):
    global bad
    bad += not ok
    print(("OK   " if ok else "FAIL ") + msg)


def validator(schema, d=None):
    if d:
        schema = {**schema["$defs"][d], "$defs": schema["$defs"], "$schema": schema["$schema"]}
    return jsonschema.Draft202012Validator(schema)


manifest = json.loads((H / "MANIFEST.json").read_text())
schemas = {}
# ---- 1) 형식
for m in manifest["schemas"]:
    n = m["name"]
    f = H / "schemas" / f"{n}.schema.json"
    s = json.loads(f.read_text())
    jsonschema.Draft202012Validator.check_schema(s)
    schemas[n] = s
    say(s["$id"] == f"{n}.schema.json" and s["title"] == n and s["$schema"] == "https://json-schema.org/draft/2020-12/schema",
        f"{n}: $id/title/$schema 규칙")
    say(all(k in m for k in ("name", "source_draft", "producer", "consumers", "unknown_keys", "note")) and m["unknown_keys"] in ("reject", "ignore"),
        f"{n}: MANIFEST 항목 필드")
files = {p.name for p in (H / "schemas").iterdir()}
say(files == {f"{m['name']}.schema.json" for m in manifest["schemas"]}, "schemas/ 파일과 MANIFEST가 일치")

# ---- 2) 예시
for kind, want_ok in (("valid", True), ("invalid", False)):
    for p in sorted((H / "examples" / kind).iterdir()):
        name, _, variant = p.stem.partition(".")
        m = next((x for x in manifest["schemas"] if x["name"] == name), None)
        if m is None:
            say(False, f"{kind}/{p.name}: MANIFEST에 없는 이름")
            continue
        d = None
        if m.get("def"):
            d = m["def"].get(variant.split("-")[0])
        errs = list(validator(schemas[name], d).iter_errors(json.loads(p.read_text())))
        ok = not errs
        say(ok == want_ok, f"{kind:7} {p.name:62} {'통과' if ok else '거부(' + errs[0].validator + ': ' + '/'.join(map(str, errs[0].absolute_path)) + ')'}")
        n_ok += ok
        n_rej += not ok
for m in manifest["schemas"]:
    say(any((H / "examples" / "invalid").glob(m["name"] + ".*")) and any((H / "examples" / "valid").glob(m["name"] + ".*")),
        f"{m['name']}: valid와 invalid 예시가 각각 1개 이상")

# ---- 3) 실제 코드
from survey_service.settings import Settings  # noqa: E402

env_ex = json.loads((H / "examples/valid/SurveyServiceEnv.json").read_text())
Settings.from_env(env_ex)
say(True, "SurveyServiceEnv 유효 예시를 실제 Settings.from_env가 받아들인다")
enum = set(schemas["SurveyServiceEnv"]["propertyNames"]["anyOf"][1]["enum"])
src = "".join((ROOT / "survey_service" / f).read_text() for f in ("settings.py", "db.py", "service.py", "api.py", "alerts.py"))
used = set(re.findall(r'"(SURVEY_[A-Z0-9_]+)"', src))
say(not (used - enum), f"코드가 읽는 SURVEY_ 이름이 모두 스키마 목록에 있다 (없는 것: {sorted(used - enum)})")
secret_names = {k[:-5] for k in enum if k.endswith("_FILE") and k != "SURVEY_RISK_RULES_FILE"}      # RISK_RULES_FILE은 비밀 짝이 아니라 경로 설정이다
say(all(k in enum for k in secret_names), "비밀 NAME_FILE 이름의 짝(NAME)이 목록에 있다")

# 실제 /readyz 본문
os.environ.setdefault("SURVEY_API_TOKEN", "x" * 16)
os.environ.setdefault("SURVEY_STAFF_API_TOKEN", "y" * 16)
from tests.fakes import FakeReleaseReader, FakeTraceIngest  # noqa: E402
from survey_service.service import App  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    t = pathlib.Path(tmp)
    st = Settings(session_db=str(t / "s.db"), outbox_db=str(t / "o.db"), work_dir=str(t / "w"), model_backend="fake")
    app = App(st, reader=FakeReleaseReader(t / "gate"), ingest=FakeTraceIngest())
    try:
        ok, body = app.readiness()
    finally:
        app.stop()
    errs = list(validator(schemas["SurveyReadyz"]).iter_errors(body))
    say(not errs, f"실제 App.readiness() 출력(ready={ok}, 키 {len(body)}개)이 SurveyReadyz를 통과 {errs[0].message[:80] if errs else ''}")
    say("SURVEY" not in json.dumps(body) and "x" * 16 not in json.dumps(body), "readiness 출력에 토큰 값이 없다")
    fixed = dict(body, ready=False, problems=[])
    say(bool(list(validator(schemas["SurveyReadyz"]).iter_errors(fixed))), "실제 출력을 ready=false/problems 없음으로 망가뜨리면 거부")

    # 실제 purge 출력
    e = {**os.environ, "SURVEY_SESSION_DB": str(t / "s2.db"), "SURVEY_OUTBOX_DB": str(t / "o2.db"), "SURVEY_ALERT_DB": str(t / "a2.db"),
         "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake", "SURVEY_WORK_DIR": str(t / "w2")}
    for args in ([], ["--session", "nope"]):
        r = subprocess.run([sys.executable, "-m", "survey_service", "purge", *args], env=e, capture_output=True, text=True, cwd=ROOT)
        line = (r.stdout.strip().splitlines() or [""])[-1]
        try:
            out = json.loads(line)
            errs = list(validator(schemas["SurveyPurgeOutput"]).iter_errors(out))
        except ValueError:
            out, errs = None, [type("E", (), {"message": "JSON 아님"})()]
        say(r.returncode == 0 and not errs, f"실제 `purge {' '.join(args)}` 종료 코드 {r.returncode}, 출력 {line[:90]}")
    r = subprocess.run([sys.executable, "-m", "survey_service", "purge"], env={k: v for k, v in e.items() if k != "SURVEY_MODEL_BACKEND"}, capture_output=True, text=True, cwd=ROOT)
    say(r.returncode == 2 and r.stdout.strip() == "", f"설정 오류면 종료 코드 2, 표준 출력 비어 있음 (실제 {r.returncode})")

# 문항 세트 해시와 문진 스키마
import survey_schema  # noqa: E402
from boundary_contracts.canonical import sha256_hash  # noqa: E402

resp = json.loads((H / "examples/valid/SurveyQuestionBankRead.response.json").read_text())
jsonschema.Draft202012Validator(survey_schema.load_schema("QuestionBank", 1)).validate(resp["question_bank"])
say(True, "question_bank가 문진 스키마 QuestionBank(정수 버전 1)를 통과")
say(resp["content_hash"] == sha256_hash(resp["question_bank"]), "content_hash == sha256_hash(question_bank) (boundary_contracts.canonical)")
bad_resp = json.loads((H / "examples/invalid/SurveyQuestionBankRead.response-bad-hash.json").read_text())
say(bad_resp.get("content_hash") != sha256_hash(bad_resp.get("question_bank", {})), "invalid 응답의 해시는 실제 해시와 다르다(스키마는 형식만 보고 값 대조는 클라이언트 몫)")

print(f"통과 {n_ok}건, 거부 {n_rej}건 (예시), 어긋남 {bad}건")
sys.exit(1 if bad else 0)
