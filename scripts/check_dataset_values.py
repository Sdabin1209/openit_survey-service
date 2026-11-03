#!/usr/bin/env python3
"""L18: 학습 데이터셋의 LoRA-A 정답 출력 값을, 문진표(QuestionBank)의 칸 정의에서 만든 서비스의 값 검증(QuestionFieldSpec + 계약 값 규칙)에 통과시켜 위반 목록을 낸다.

사용: python scripts/check_dataset_values.py <bank.json> <lora_a_*.jsonl ...>   (표준 출력에 JSON 요약, 위반 사례는 --examples N개까지)
검증은 서비스가 실제로 쓰는 것과 같다: field_specs.build_field_specs -> boundary_contracts.action_rules.apply_value_rules.
추가로 서비스가 값 규칙 앞에서 보는 것 둘(칸이 문진표에 있는가, 반복 키가 문항의 repeat_keys에 있는가)을 센다. 데이터는 읽기만 한다.
"""
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from boundary_contracts.action_rules import apply_value_rules  # noqa: E402
from survey_service.components.field_specs import build_field_specs, invalid_field_specs  # noqa: E402
from survey_service.components.flow_table import flow_problems  # noqa: E402

BRACKET = re.compile(r"\[[^\]]*\]")
KEY = re.compile(r"^([A-Za-z0-9_.]+?)(?:\[([^\]]*)\])?\.([A-Za-z0-9_]+)$")


def main(argv):
    ex_n = 5
    if "--examples" in argv:
        i = argv.index("--examples"); ex_n = int(argv[i + 1]); del argv[i:i + 2]
    bank = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    fields = build_field_specs(bank)
    rk = {q["question_id"]: set(q["repeat_keys"]) for q in bank["questions"]}
    spec = {f["slot"] for f in fields["fields"]}
    out = {"bank": {"question_set_id": bank["question_set_id"], "slots": len(spec), "invalid_field_specs": invalid_field_specs(bank), "flow_problems": flow_problems(bank)},
           "files": {}, "violations": collections.Counter(), "rewrites": collections.Counter(), "by_slot": collections.Counter(), "examples": []}
    for fn in argv[1:]:
        n = vals = 0
        for line in Path(fn).read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            msgs = rec["messages"]
            inp = json.loads(next(m["content"] for m in msgs if m["role"] == "user"))
            gold = json.loads(next(m["content"] for m in msgs if m["role"] == "assistant"))
            n += 1
            found = []
            for v in gold.get("values") or []:
                if v.get("save") is not True or v.get("value") is None:
                    continue
                vals += 1
                m = KEY.match(v["key"])
                if not m or f"{m.group(1)}.{m.group(3)}" not in spec:
                    found.append(("UNKNOWN_SLOT", v["key"]))
                elif (m.group(2) is None) == bool(rk.get(m.group(1))) or (m.group(2) is not None and m.group(2) not in rk[m.group(1)]):
                    found.append(("BAD_REPEAT_KEY", v["key"]))
            fixed, viol, reasons = apply_value_rules(inp, gold, fields)
            found += [(f"VIOLATION:{x.code}", gold["values"][int(x.path.split('/')[1])]["key"]) for x in viol]
            for r in reasons:
                out["rewrites"][r] += 1
                found.append((f"REWRITE:{r}", ""))
            for code, key in found:
                if not code.startswith("REWRITE"):
                    out["violations"][code] += 1
                    out["by_slot"][f"{code} {BRACKET.sub('', key)}"] += 1
                if len(out["examples"]) < ex_n:
                    out["examples"].append({"file": Path(fn).name, "scenario_id": rec.get("scenario_id"), "code": code, "key": key})
        out["files"][Path(fn).name] = {"records": n, "saved_values": vals}
    for k in ("violations", "rewrites", "by_slot"):
        out[k] = dict(out[k].most_common())
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
