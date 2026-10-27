"""전수 점검 지적의 수정 시험: CR-D25(NOT_COMMITTED 재처리), 필수 고정 문장 적재 검사 등."""
import copy

from survey_service.inference import InferenceServer
from survey_service.release import ReleaseLoader, ReleaseRegistry

from .e2e_support import ANSWER_OUT, Rig
from .fakes import FakeReleaseReader
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED


def test_not_committed_turn_is_reprocessed_on_same_turn_id():
    """CR-D25: 모델 단계 실패로 확정되지 못한 턴은 결과를 고정하지 않는다. 같은 turn_id 재전송이 새로 처리되어 확정된다."""
    rig = Rig()
    rig.start()
    before = rig.doc()["answer_records"][0]["current_value"]
    out = rig.say("안 피움", out="깨진 출력 {{", turn_id="m-2")                    # 추출이 깨져 두 번 모두 검증 실패
    rig.model.queue.clear()
    sig = out.frontend_input["turn_failure_signal"]
    assert sig and sig["write_committed"] == "NOT_COMMITTED" and rig.doc()["answer_records"][0]["current_value"] == before
    assert rig.store.get_turn("S1", "m-2") is None
    again = rig.say("안 피움", out=ANSWER_OUT, turn_id="m-2")                      # 모델이 복구된 뒤 같은 turn_id
    assert again.frontend_input["turn_failure_signal"] is None and not again.replayed
    assert rig.doc()["answer_records"][0]["current_value"] == {"answer": False}
    third = rig.say("안 피움", turn_id="m-2")                                        # 확정된 턴은 저장된 결과를 그대로(멱등)
    assert third.replayed and third.frontend_input == again.frontend_input


def _loader(tmp_path, bank):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", bank, turn_engine_version=INSTALLED["turn_engine_version"], model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    reg = ReleaseRegistry()
    return reg, ReleaseLoader(reader, reg, InferenceServer("fake"), workdir=tmp_path / "work", installed_versions=INSTALLED, retry_base_seconds=0)


def test_release_without_required_fixed_sentence_is_rejected_with_reason(tmp_path):
    bank = copy.deepcopy(sample_bank())
    bank["session_settings"]["fixed_sentences"] = [f for f in bank["session_settings"]["fixed_sentences"] if f["key"] != "RISK_NOTICE"]
    reg, loader = _loader(tmp_path, bank)
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "MISSING_FIXED_SENTENCE" and reg.active_id() is None
    assert "RISK_NOTICE" in loader.status.rejections[-1]["detail"]


def test_release_with_blank_fixed_sentence_is_rejected(tmp_path):
    bank = copy.deepcopy(sample_bank())
    for f in bank["session_settings"]["fixed_sentences"]:
        if f["key"] == "CALL_STAFF":
            f["sentence"] = "  "
    reg, loader = _loader(tmp_path, bank)
    assert loader.poll_once().code == "MISSING_FIXED_SENTENCE"
