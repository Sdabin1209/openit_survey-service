#!/usr/bin/env python3
"""서비스의 표 찾기(`ResponseTables.b_fields`)가 학습 데이터의 B 입력(next 제외)과 글자 단위로 같은 문장을 내는지 전수로 센다.

학습 서버 파일은 읽기 전용이다. 표준출력으로 흘려 이 PC에서만 계산하고 저장하지 않는다(서버에는 cat만). 예(같은 split의 A·B 파일):

    python scripts/table_agreement.py <(ssh openit 'cat ~/WS/dataset/data/final/lora_a_val.jsonl') \\
                                      <(ssh openit 'cat ~/WS/dataset/data/final/lora_b_val.jsonl')

A 행(입력+정답)과 B 행은 scenario_id와 환자 말·action·reason으로 짝짓는다. START 행은 A가 없어 topic과 next만 본다.
출력은 필드별 대조 수와 불일치 수, 불일치 예 몇 건(문장 포함: 가공 데이터지만 환자 발화는 없다). 서비스가 따르는 기준은 데이터셋 담당의 규칙(labels.py)이고,
이 일치는 '학습 데이터와 같은 문장을 낸다'는 뜻이다(표 승인 여부와는 별개).
"""
from __future__ import annotations

import collections
import json
import sys

import survey_schema
from survey_service.response_tables import ResponseTables

FIELDS = ("action", "reason", "topic", "user_utterance", "known", "ask", "previous", "current", "hold", "note")


def rows(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                yield r.get("scenario_id"), json.loads(r["messages"][1]["content"]), r["messages"][2]["content"]


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    tables = ResponseTables(survey_schema.load_data("response_tables"))
    a_rows = collections.defaultdict(list)
    for sid, ai, ao in rows(argv[1]):
        a_rows[sid].append((ai, json.loads(ao)))
    tot, bad = collections.Counter(), collections.defaultdict(list)
    for sid, bi, _ in rows(argv[2]):
        tot["B 행"] += 1
        if bi["action"] == "START":
            ok = bi.get("topic") == tables.topic("Q1.D01") and bi["next"]["question_id"] == "Q1.D01" and bi["next"]["type"] == "NEXT"
            tot["START 일치" if ok else "START 불일치"] += 1
            continue
        c = [x for x in a_rows.get(sid, []) if x[0]["user_utterance"] == bi["user_utterance"] and x[1]["action"] == bi["action"] and x[1].get("reason") == bi.get("reason")]
        if len(c) != 1:
            tot[f"짝 {len(c)}개"] += 1
            if not c:
                continue
        ai, ao = c[0]
        got = tables.b_fields(ao, ai["question_id"], ai["user_utterance"], ai["state"])
        want = {k: v for k, v in bi.items() if k != "next"}
        tot["대조한 행"] += 1
        tot["일치 행" if got == want else "불일치 행"] += 1
        for k in FIELDS:
            if k in got or k in want:
                tot[f"필드 {k} 대조"] += 1
                if got.get(k) != want.get(k):
                    tot[f"필드 {k} 불일치"] += 1
                    if len(bad[k]) < 3:
                        bad[k].append((got.get(k), want.get(k), ao.get("reason"), ao.get("targets")))
    for k in sorted(tot):
        print(f"{k}: {tot[k]}")
    print("폴백 카운터:", tables.stats)
    for k, v in bad.items():
        print("---", k)
        for g, w, r, t in v:
            print("  서비스:", g)
            print("  데이터:", w, "|", r, t)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
