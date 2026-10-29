#!/usr/bin/env python3
"""데이터셋 담당의 학습 데이터(C1 빌드)와 서비스 규칙의 대조(읽기 전용, 데이터는 리포에 넣지 않는다).

환경 변수 `SURVEY_REFERENCE_DATA_DIR` = `lora_{a,b}_{train,val,test}.jsonl`이 있는 폴더(예: .../openit-server/data/final).
실행: `SURVEY_REFERENCE_DATA_DIR=... PYTHONPATH=. .venv/bin/python scripts/reference_agreement.py [--splits val,test] [--max N]`

(1) B 입력: `ResponseTables.b_fields`가 만든 필드(next 제외)가 B 입력과 글자 단위로 같은가(분모: B 전체 행, START 포함).
(2) A 입력 state: 서비스 규칙(`extraction.build_state`)이 만든 state가 A 입력의 state와 같은가(키 순서까지, 같은 칸 집합·값 대조 포함).
(3) 값 검증: A 출력(정답)을 서비스의 검증 경로(계약의 `check_action_extraction` + `check_slots` + `check_values`)에 넣어 정상 정답을 거부하는 비율과 사유.
문항 세트는 이 영역의 시험용 세트(tests/sample_bank.py: 31개 문항·칸, 선택지·단위 정의 없음)다. 데이터 쪽 question_bank.json은 문장만 있어 칸 정의를 담지 않는다.
"""
from __future__ import annotations

import argparse
import collections
import copy
import json
import os
import sys
from pathlib import Path

ENV = "SURVEY_REFERENCE_DATA_DIR"


def rows(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                yield r.get("scenario_id"), json.loads(r["messages"][1]["content"]), r["messages"][2]["content"]


def b_agreement(d: Path, splits, tables, limit):
    tot = collections.Counter()
    bad = collections.defaultdict(list)
    for sp in splits:
        a_rows = collections.defaultdict(list)
        for sid, ai, ao in rows(d / f"lora_a_{sp}.jsonl"):
            a_rows[sid].append((ai, json.loads(ao)))
        n = 0
        for sid, bi, _ in rows(d / f"lora_b_{sp}.jsonl"):
            if limit and n >= limit:
                break
            n += 1
            tot[f"{sp} B 행"] += 1
            tot["B 행(전체)"] += 1
            if bi["action"] == "START":
                ok = bi.get("topic") == tables.topic("Q1.D01") and bi["next"]["question_id"] == "Q1.D01" and bi["next"]["type"] == "NEXT"
                tot["일치(START)" if ok else "불일치(START)"] += 1
                continue
            c = [x for x in a_rows.get(sid, []) if x[0]["user_utterance"] == bi["user_utterance"] and x[1]["action"] == bi["action"] and x[1].get("reason") == bi.get("reason")]
            if len(c) != 1:
                tot[f"짝을 못 찾음({len(c)}개)"] += 1
                continue
            ai, ao = c[0]
            got = tables.b_fields(ao, ai["question_id"], ai["user_utterance"], ai["state"])
            want = {k: v for k, v in bi.items() if k != "next"}
            if got == want:
                tot["일치"] += 1
            else:
                tot["불일치"] += 1
                for k in sorted(set(got) | set(want)):
                    if got.get(k) != want.get(k):
                        tot[f"불일치 필드 {k}"] += 1
                        if len(bad[k]) < 3:
                            bad[k].append((sid, got.get(k), want.get(k)))
    return tot, bad


def doc_from_input(inp, bank, flow):
    from survey_service.store import effects
    doc = effects.initial_state("S", bank.raw, inp["question_id"])
    recs = {effects.slot_key(r["slot"]): r for r in doc["answer_records"]}
    for key, ent in inp["state"].items():
        ref = effects.slot_ref(key)
        r = recs.get(effects.slot_key(ref))
        if r is None:
            continue
        if ent["status"] == "ANSWERED" and ent.get("value") is not None:
            r["current_value"] = {"answer": ent["value"]}
            r["precision"] = "exact"
        elif ent["status"] in ("MISSING", "UNCLEAR"):
            doc["progress_record"]["open_slots"].append({"key": key, "status": ent["status"]})
    return doc


def a_state_agreement(d: Path, splits, bank, flow, limit):
    from survey_service import extraction
    tot = collections.Counter()
    ex = collections.defaultdict(list)
    for sp in splits:
        n = 0
        for sid, inp, _ in rows(d / f"lora_a_{sp}.jsonl"):
            if limit and n >= limit:
                break
            n += 1
            tot["A 행(전체)"] += 1
            doc = doc_from_input(inp, bank, flow)
            got = extraction.build_state(doc, flow)
            want = inp["state"]
            if json.dumps(got, ensure_ascii=False) == json.dumps(want, ensure_ascii=False):
                tot["일치(순서·값 모두)"] += 1
                continue
            gk, wk = list(got), list(want)
            if set(gk) == set(wk) and all(got[k] == want[k] for k in gk):
                item = lambda ks: [k.rsplit(".", 1)[0].split("[")[0] for k in ks]
                cause = "같은 칸·값, 문항 묶음 순서만 다름" if item(gk) != item(wk) else "같은 칸·값, 같은 문항 안 칸 순서만 다름"
                order = list(flow.table.order)
                its = [i for n_, i in enumerate(item(wk)) if n_ == 0 or item(wk)[n_ - 1] != i]
                tot["  (순서만 다른 행 중 데이터 순서가 문항 흐름 순서와 같음)" if its == sorted(its, key=order.index) and len(set(its)) == len(its) else "  (순서만 다른 행 중 데이터 순서가 흐름 순서도 아님: 생성 순서 산물)"] += 1
            elif set(gk) == set(wk):
                cause = "같은 칸, 값 또는 상태가 다름"
            elif set(gk) < set(wk):
                cause = "서비스 state가 데이터보다 적음(데이터에만 있는 칸)"
            elif set(gk) > set(wk):
                cause = "서비스 state가 데이터보다 많음(서비스에만 있는 칸)"
            else:
                cause = "칸 집합이 서로 다름"
            tot["불일치: " + cause] += 1
            if len(ex[cause]) < 3:
                ex[cause].append((sid, inp["question_id"], gk, wk))
    return tot, ex


def validation_check(d: Path, splits, bank, flow, limit):
    from boundary_contracts.action_rules import check_action_extraction
    from survey_service import extraction
    tot = collections.Counter()
    codes = collections.Counter()
    ex = collections.defaultdict(list)
    for sp in splits:
        n = 0
        for sid, inp, ao in rows(d / f"lora_a_{sp}.jsonl"):
            if limit and n >= limit:
                break
            n += 1
            out = json.loads(ao)
            tot["A 정답 행(전체)"] += 1
            tot[f"{sp} 행"] += 1
            doc = doc_from_input(inp, bank, flow)
            viol = []
            viol += [("engine", v.code, v.path) for v in check_action_extraction(inp, out, flow.table.wire())]
            viol += [("slots", v.code, v.path) for v in extraction.check_slots(out, doc["answer_records"])]
            viol += [("values", v.code, v.path) for v in extraction.check_current_item(out, doc, flow)]
            if not viol:
                tot["통과"] += 1
                continue
            tot["거부"] += 1
            for layer, code, path in {(a, b, "") for a, b, _ in viol}:
                codes[f"{layer}:{code}"] += 1
            key = viol[0][0] + ":" + viol[0][1]
            if len(ex[key]) < 4:
                ex[key].append((sid, inp["question_id"], out["action"], out.get("next_item"), [(v["key"], v["value"], v["status"], v["save"]) for v in out["values"]][:4], viol[:2]))
    return tot, codes, ex


# 수정 전(2차 점검 전) 임시 기본 목록과 매칭 방식(부분 문자열, 공백 제거 후 매칭, 부정 처리 없음). 비교용으로 얼려 둔 사본이다.
_OLD_PROVISIONAL = {'self-harm': ('자살', '죽고싶', '죽을래', '죽어버리', '죽고만싶', '스스로목숨', '목숨을끊', '자해', '손목을그', '극단적선택', '살기싫', '사는게싫', '삶을끝'), 'chest-pain': ('가슴이아', '가슴이너무아', '흉통', '가슴통증', '가슴이조이', '가슴이답답', '가슴을쥐어', '심장이아', '심장이조여', '심장이터'), 'dyspnea': ('숨이안쉬', '숨을못쉬', '숨쉬기힘', '숨쉬기가힘', '숨쉬기어렵', '호흡곤란', '숨이막', '숨막혀', '숨이차서말', '숨이넘어'), 'consciousness': ('의식을잃', '의식이없', '정신을잃', '정신을못차', '기절', '쓰러졌', '쓰러질것', '쓰러질거', '의식이흐', '정신이혼미'), 'bleeding': ('피가많이', '피가계속나', '피가멈추지', '출혈이심', '피를토', '토혈', '피를많이', '하혈이심', '피가쏟'), 'paralysis': ('마비', '팔다리에힘이안', '한쪽팔이안', '한쪽다리가안', '말이안나', '말이어눌', '말이잘안나', '입이돌아', '얼굴이한쪽', '한쪽이저리'), 'help': ('살려', '도와주세요', '응급', '119', '구급차')}


def _old_match(text: str) -> bool:
    import re
    import unicodedata
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFC", text).casefold())
    return any(p.replace(" ", "") in compact for ph in _OLD_PROVISIONAL.values() for p in ph)


def false_alarm_check(d: Path, splits, limit):
    """모든 user_utterance에 수정 전·후 매처를 돌려 적중 수(분모 포함)와 적중 발화를 센다. 정상 발화 데이터이므로 적중은 오탐 후보다."""
    from survey_service.risk_rules import RuleRiskSource
    new = RuleRiskSource.provisional_default()
    tot = collections.Counter()
    hits_new, hits_old_only = [], []
    for sp in splits:
        n = 0
        for sid, inp, _ in rows(d / f"lora_a_{sp}.jsonl"):
            if limit and n >= limit:
                break
            n += 1
            u = inp["user_utterance"]
            tot["발화(전체)"] += 1
            o, w = _old_match(u), new.match(u)
            tot["수정 전 적중"] += bool(o)
            tot["수정 후 적중"] += bool(w)
            if w:
                hits_new.append((sid, w, u))
            elif o:
                hits_old_only.append((sid, u))
    tot["수정 후: 부정으로 제외한 적중"] = new.context_hits.get("negated_hits", 0)
    tot["수정 후: 제3자 이야기로 제외한 적중"] = new.context_hits.get("third_party_hits", 0)
    tot["수정 후: 과거 병력 이야기로 제외한 적중"] = new.context_hits.get("history_hits", 0)
    return tot, hits_new, hits_old_only


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", default="train,val,test")
    ap.add_argument("--max", type=int, default=0, help="split마다 앞 N행만(0이면 전체)")
    ap.add_argument("--only", default="1,2,3")
    a = ap.parse_args(argv)
    d = Path(os.environ.get(ENV, ""))
    if not os.environ.get(ENV) or not (d / "lora_a_val.jsonl").exists():
        print(f"{ENV}가 없거나 데이터가 없다: 건너뛴다")
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import survey_schema
    from survey_service.assembly import Bank
    from survey_service.flow import load_flow
    from survey_service.response_tables import ResponseTables
    from tests.sample_bank import sample_bank
    splits = a.splits.split(",")
    flow, bank = load_flow(), Bank(sample_bank())
    tables = ResponseTables(survey_schema.load_data("response_tables"))
    only = a.only.split(",")
    if "4" in only:
        tot, hn, ho = false_alarm_check(d, splits, a.max)
        print("== (4) 위험 문구 오탐(수정 전·후 매처) ==")
        for k in tot:
            print(f"{k}: {tot[k]}")
        print("-- 수정 후에도 적중한 발화(최대 200)")
        for sid, rid, u in hn[:200]:
            print(f"  [{rid}] {sid}: {u}")
        print("-- 수정 전에만 적중한 발화(최대 30)")
        for sid, u in ho[:30]:
            print(f"  {sid}: {u}")
    if "1" in only:
        tot, bad = b_agreement(d, splits, tables, a.max)
        print("== (1) B 입력 대조 ==")
        for k in sorted(tot):
            print(f"{k}: {tot[k]}")
        for k, v in bad.items():
            print("--", k, v[:3])
    if "2" in only:
        tot, ex = a_state_agreement(d, splits, bank, flow, a.max)
        print("== (2) A 입력 state 대조 ==")
        for k in sorted(tot):
            print(f"{k}: {tot[k]}")
        for k, v in ex.items():
            print("--", k)
            for e in v[:3]:
                print("   ", e)
    if "3" in only:
        tot, codes, ex = validation_check(d, splits, bank, flow, a.max)
        print("== (3) 정상 정답의 값 검증 거부 ==")
        for k in sorted(tot):
            print(f"{k}: {tot[k]}")
        for k, v in codes.most_common():
            print(f"사유 {k}: {v}")
        for k, v in ex.items():
            print("--", k)
            for e in v:
                print("   ", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
