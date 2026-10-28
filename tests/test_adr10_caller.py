"""ADR 0010(호출자 쪽): 서버가 보증한 LoRA-A 출력의 의미 규칙은 서비스가 다시 검사하지 않고(R7), turn-engine은 코드 의존이 아니다(D109).
기대값은 계약 문서·스키마(boundary-contracts ExtractRequest, QuestionFlowTable)와 문진 스키마(question_flow.json)에서 온다."""
import subprocess
import sys
from pathlib import Path

import survey_schema
from boundary_contracts import validator

from survey_service.settings import Settings

from .e2e_support import Rig, ext, val

ROOT = Path(__file__).resolve().parents[1]


def test_extract_request_to_the_server_is_extract_request_with_the_schema_flow(tmp_path):
    from .test_inference_client import Env
    e = Env(tmp_path)
    try:
        assert e.loader.poll_once().action == "LOADED"
        e.start()
        e.turn("안녕하세요")
        body = e.srv.extract_bodies[-1]
        flow_data = survey_schema.load_data("question_flow")
        assert list(validator("ExtractRequest").iter_errors(body)) == []
        assert body["flow"] == {"order": flow_data["order"], "skips": flow_data["skips"]}      # 서비스 고유 필드(reference_sources 등)는 싣지 않는다
    finally:
        e.close()


def test_server_rule_violation_is_output_invalid_then_retried_once_by_the_contract_sentence(tmp_path):
    """계약 I9 '서비스가 지키는 것': 서버가 규칙 위반 출력을 503 OUTPUT_INVALID로 내면 같은 요청을 한 번 더 보내고, 그래도면 턴 실패(NOT_COMMITTED)."""
    from .test_inference_client import Env, failed
    e = Env(tmp_path)
    try:
        assert e.loader.poll_once().action == "LOADED"
        e.start()
        e.srv.extract_fn = lambda req: {"action": "REASK", "next_item": "Q1.D03", "reason": "MISSING", "values": [], "targets": []}     # REASK인데 next_item이 다르다
        o = e.turn("음")
        assert len(e.srv.extract_bodies) == 2 and e.srv.extract_bodies[0] == e.srv.extract_bodies[1]
        assert failed(o) == [{"phase": "BEFORE_COMMIT", "step": "LoRA-A"}] and o.trace["extract_status"] == "OUTPUT_INVALID"
    finally:
        e.close()


# 첫 문항에서 REASK인데 next_item이 다른 문항: 예전에는 서비스가 NEXT_ITEM_MISMATCH로 거부하고 같은 요청을 한 번 더 보냈다
VIOLATING = ext("REASK", "Q1.D03", val("Q1.D01.on_medication", None, "MISSING"), reason="MISSING", targets=["Q1.D01.on_medication"])


def test_service_does_not_revalidate_or_retry_on_server_guaranteed_rules():
    rig = Rig()
    rig.start()
    rig.model.queue.append(VIOLATING)
    o = rig.say("음")
    assert len(rig.session.extract_requests) == 1 and o.trace["extract_attempts"] == 1
    assert not any(v["code"] == "NEXT_ITEM_MISMATCH" for v in (o.trace["validation_violations"] or []))


def test_turn_engine_version_comes_from_settings_not_from_a_library():
    env = {"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake"}
    assert Settings.from_env(env).turn_engine_version == "0.4.3"                       # 헤드 기준(D109)
    assert Settings.from_env({**env, "SURVEY_TURN_ENGINE_VERSION": "0.5.1"}).turn_engine_version == "0.5.1"


def test_service_imports_without_turn_engine():
    code = ("import sys\n"
            "class B:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] == 'turn_engine': raise ImportError('turn_engine 금지')\n"
            "sys.meta_path.insert(0, B())\n"
            "import survey_service.service, survey_service.graph, survey_service.flow, survey_service.release.loader\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
