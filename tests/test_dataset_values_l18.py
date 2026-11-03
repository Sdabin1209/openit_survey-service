"""L18: scripts/check_dataset_values.py가 위반을 실제로 잡는다(합성 정답으로). 실제 데이터셋 결과는 docs/L18-dataset-values.md."""
import json
import subprocess
import sys
from pathlib import Path

from .sample_bank import sample_bank

ROOT = Path(__file__).resolve().parent.parent


def rec(key, value, qid="Q8_1", state=None):
    inp = {"current_question": "x", "question_id": qid, "state": state or {}, "user_utterance": "y"}
    out = {"action": "NEXT", "next_item": "Q8_2", "values": [{"key": key, "value": value, "precision": "exact", "save": True, "status": "ANSWERED"}]}
    return {"scenario_id": "s", "messages": [{"role": "user", "content": json.dumps(inp)}, {"role": "assistant", "content": json.dumps(out)}]}


def run(tmp_path, *recs):
    (tmp_path / "bank.json").write_text(json.dumps(sample_bank(question_set_id="health-screening")), encoding="utf-8")
    (tmp_path / "a.jsonl").write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts/check_dataset_values.py"), str(tmp_path / "bank.json"), str(tmp_path / "a.jsonl")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_clean_records_have_no_violations(tmp_path):
    out = run(tmp_path, rec("Q8_1.days", 3))
    assert out["violations"] == {} and out["files"]["a.jsonl"] == {"records": 1, "saved_values": 1}


def test_unknown_slot_wrong_type_below_minimum_and_bad_repeat_key_are_found(tmp_path):
    out = run(tmp_path, rec("Q8_1.nope", 3), rec("Q8_1.days", "많이"), rec("Q8_1.days", -1), rec("Q7_1[soju2].amount", 1, "Q7_1"), rec("Q7_1.amount", 1, "Q7_1"))
    assert set(out["violations"]) >= {"UNKNOWN_SLOT", "BAD_REPEAT_KEY"} and len(out["violations"]) >= 3
    assert sum(out["violations"].values()) == 5
