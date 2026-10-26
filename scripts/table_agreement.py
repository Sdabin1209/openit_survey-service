#!/usr/bin/env python3
"""표 초안(schema/data/response_tables.json)이 학습 데이터셋 v2의 B 입력과 얼마나 같은 문장을 내는지 센다.

데이터는 저장하지 않고 표준 입력·프로세스 치환으로만 읽는다(학습 서버 읽기 전용 규칙). 예(두 파일은 같은 split):

    python scripts/table_agreement.py <(ssh openit 'cat ~/taehyeon/data/dataset_10_14/lora_a_val.jsonl') \\
                                      <(ssh openit 'cat ~/taehyeon/data/dataset_10_14/lora_b_val.jsonl')

A 행과 B 행은 환자 말·action·reason으로 짝짓는다. 출력은 필드별 일치 수/전체와 틀린 예 몇 건이다. 일치율은 표를 만든 데이터에 대한 것(in-sample)이라
표가 옳다는 증거가 아니라 "데이터와 같은 문장을 낸다"는 뜻이다. 이웃 영역의 표가 정해지면 그 표로 바꿔 다시 센다.
"""
from __future__ import annotations

import collections
import json
import sys

import survey_schema
from survey_service import extraction
from survey_service.response_tables import ResponseTables, item_of


def rows(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            m = r["messages"]
            out.append((json.loads(m[1]["content"]), m[2]["content"]))
    return out


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    tables = ResponseTables(survey_schema.load_data("response_tables"))
    a_rows = {}
    for ai, ao in rows(argv[1]):
        a_rows.setdefault(ai["user_utterance"], []).append((ai, json.loads(ao)))
    res = collections.defaultdict(lambda: [0, 0])
    bad = collections.defaultdict(list)

    def chk(name, got, want, ctx):
        res[name][1] += 1
        if got == want:
            res[name][0] += 1
        else:
            bad[name].append((got, want, ctx))
    unmatched = 0
    for bi, _ in rows(argv[2]):
        if bi["action"] == "START":
            continue
        c = [x for x in a_rows.get(bi["user_utterance"], []) if x[1]["action"] == bi["action"] and x[1].get("reason") == bi.get("reason")]
        if not c:
            unmatched += 1
            continue
        ai, ao = c[0]
        state = {k: v["value"] for k, v in ai["state"].items()}
        vals, act = ao["values"], bi["action"]
        chk("topic", tables.topic(item_of(vals[0]["key"]) if vals else ai["question_id"]), bi["topic"], ao["next_item"])
        saved = [v for v in vals if v["save"]]
        if act in ("NEXT", "CORRECT", "REASK"):
            chk("known_" + act, tables.known(saved, state), bi["known"], [(v["key"], v["value"], v["status"]) for v in vals])
        if act in ("HOLD", "SKIP"):
            chk("hold", tables.hold(ao["targets"]), bi["hold"], ao["targets"])
        if act in ("REASK", "CONFIRM"):
            chk("ask_" + act, tables.ask(ao["reason"], ao["targets"], vals, state), bi["ask"], (ao["reason"], ao["targets"]))
        if act == "CONFIRM":
            prev, cur = extraction._confirm_texts(tables, ao, ai["state"])
            chk("previous", prev, bi["previous"], ao["reason"])
            chk("current", cur, bi["current"], ao["reason"])
    for k, (ok, n) in sorted(res.items()):
        print(f"{k:14s} {ok}/{n}  {100 * ok / n:.1f}%")
    print("짝을 못 찾은 B 행:", unmatched)
    for k in sorted(bad):
        print("---", k, len(bad[k]))
        for g, w, c in bad[k][:3]:
            print("   got ", g)
            print("   want", w)
            print("   ctx ", str(c)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
