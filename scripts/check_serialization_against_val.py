#!/usr/bin/env python3
"""서비스가 만든 모델 입력이 학습 데이터 val 행과 글자 단위로 같은지 센다 (D77).

학습 서버 파일은 읽기 전용이다. 표준출력으로 흘려 이 PC에서만 처리하고 저장하지 않는다. 예:

    python scripts/check_serialization_against_val.py <(ssh openit 'cat ~/taehyeon/data/dataset_10_14/lora_a_val.jsonl') \\
                                                      <(ssh openit 'cat ~/taehyeon/data/dataset_10_14/lora_b_val.jsonl')

A: 각 val 행의 state에서 세션 문서를 거꾸로 만들고(저장된 값, 열린 칸, 직전 응답) `extraction.build_extract_input`으로 입력 객체를 만든 뒤
   (1) 객체가 같은지, (2) `json.dumps(ensure_ascii=False)` 글자가 같은지, (3) model-runtime prompt-4 렌더러의 user 메시지가 val의 user 메시지와 같은지.
B: A 행과 B 행을 환자 말·action·reason으로 짝지어, A 출력으로 `build_response_request`를 돌려 (a) 키 집합 (b) 값 형태(타입) (c) 전체 직렬화 글자를 센다.
본문은 출력하지 않고 행 번호와 어긋난 키 이름만 낸다. 문서를 거꾸로 만든 것이므로 state를 서비스 규칙이 다시 만들 수 있는 행만 일치로 센다."""
from __future__ import annotations

import collections
import json
import sys

import survey_schema
from model_runtime import prompts

from survey_service.components import extraction_check as extraction
from survey_service.components.question_bank_view import Bank
from survey_service.components.flow_table import load_flow
from survey_service.components.response_request import ResponseTables
from survey_service.components import session_store as effects
from tests.sample_bank import sample_bank

FLOW, TABLES, BANK = load_flow(), ResponseTables(survey_schema.load_data("response_tables")), Bank(sample_bank())


def read(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                m = json.loads(line)["messages"]
                out.append((m[1]["content"], json.loads(m[1]["content"]), m[2]["content"]))
    return out


def doc_for(ai):
    doc = effects.initial_state("S", sample_bank(), ai["question_id"])
    ups, opens = [], []
    for k, v in ai["state"].items():
        if v["status"] == "ANSWERED":
            ups.append({"slot": effects.slot_ref(k), "action": "FIRST_WRITE", "value": v["value"], "precision": "exact"})
        else:
            opens.append({"key": k, "status": v["status"]})
    doc["answer_records"] = effects.apply_answer_updates(doc["answer_records"], ups)
    doc["progress_record"]["open_slots"] = opens
    doc["dialogue_log"].append({"response_text": ai["current_question"]})
    return doc


def shape(x):
    if isinstance(x, dict):
        return {k: shape(v) for k, v in x.items()}
    if isinstance(x, list):
        return [shape(v) for v in x]
    return type(x).__name__


def keyset(x, p=""):
    if isinstance(x, dict):
        s = set()
        for k, v in x.items():
            s |= {p + k} | keyset(v, p + k + ".")
        return s
    return set()


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    a_rows, b_rows = read(argv[1]), read(argv[2])
    c = collections.Counter()
    diff_keys = collections.Counter()
    bad_rows = []
    pairs = collections.defaultdict(list)
    for i, (user, ai, ao) in enumerate(a_rows, 1):
        c["A rows"] += 1
        try:
            got = extraction.build_extract_input(doc_for(ai), BANK, FLOW, ai["user_utterance"])
        except Exception as e:      # 문서를 만들 수 없는 행(문항 세트에 없는 칸 등)
            c["A 재구성 불가"] += 1
            bad_rows.append(f"A행 {i} 재구성 불가 {type(e).__name__}")
            continue
        c["A 객체 같음"] += got == ai
        c["A state 키 순서 같음"] += list(got["state"]) == list(ai["state"])
        c["A state 키 집합 같음"] += set(got["state"]) == set(ai["state"])
        c["A 값 내부 키 순서 같음"] += all(list(got["state"][k]) == list(ai["state"][k]) for k in got["state"] if k in ai["state"])
        c["A json 글자 같음"] += json.dumps(got, ensure_ascii=False) == user
        c["A prompt-4 user 글자 같음"] += prompts._USER["prompt-4"]("LORA_A", got) == user
        if json.dumps(got, ensure_ascii=False) != user:
            diff_keys["A:" + ("state 키 집합" if set(got["state"]) != set(ai["state"]) else "state 순서/값")] += 1
            bad_rows.append(f"A행 {i}")
        pairs[(ai["user_utterance"], ao_ := json.loads(ao)["action"], json.loads(ao).get("reason"))].append((ai, json.loads(ao), got))
    for j, (user, bi, _) in enumerate(b_rows, 1):
        c["B rows"] += 1
        if bi["action"] == "START":
            got = extraction.build_start_request(BANK, TABLES, bi["next"]["question_id"])
            c["B START 글자 같음(next.text 제외 키)"] += keyset(got) == keyset(bi)
            continue
        cand = pairs.get((bi["user_utterance"], bi["action"], bi.get("reason")))
        if not cand:
            c["B 짝 없음"] += 1
            continue
        ai, ao, a_got = cand[0]
        c["B 짝 있음"] += 1
        doc = doc_for(ai)
        w = extraction.plan_writes(doc, ao)
        after = dict(doc)
        after["answer_records"] = effects.apply_answer_updates(doc["answer_records"], w["answer_updates"])
        try:
            got = extraction.build_response_request(ao, ai, after, BANK, TABLES)
        except Exception as e:
            c["B 조립 실패"] += 1
            bad_rows.append(f"B행 {j} 조립 실패 {type(e).__name__}")
            continue
        ks_ok = keyset(got) == keyset(bi)
        c["B 키 집합 같음"] += ks_ok
        c["B 값 형태 같음"] += ks_ok and shape(got) == shape(bi)
        txt_ok = json.dumps(got, ensure_ascii=False) == user
        c["B json 글자 같음"] += txt_ok
        c["B prompt-4 user 글자 같음"] += prompts._USER["prompt-4"]("LORA_B", got) == user
        if not ks_ok:
            for k in sorted(keyset(got) ^ keyset(bi)):
                diff_keys[f"B:{bi['action']}:{k}"] += 1
        elif not txt_ok:
            for k in sorted(keyset(got)):
                g, b = got, bi
                try:
                    for part in k.split("."):
                        g, b = g[part], b[part]
                except (KeyError, TypeError):
                    continue
                if not isinstance(g, dict) and g != b:
                    diff_keys[f"B:{bi['action']}:값 다름:{k}"] += 1
            bad_rows.append(f"B행 {j} {bi['action']}")
    for k, v in c.items():
        print(f"{k}: {v}")
    print("--- 어긋난 키/원인 ---")
    for k, v in diff_keys.most_common(30):
        print(f"  {k}: {v}")
    print("어긋난 행 수:", len(bad_rows), bad_rows[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
