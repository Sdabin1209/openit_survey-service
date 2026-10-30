"""릴리스 로더: 포인터 폴링, 적재, 거부와 이전 릴리스 유지, 새 세션부터 적용."""
import pytest
from boundary_contracts import PIPELINE_SCHEMA_VERSION, BoundaryError

import model_runtime
from survey_service.wiring.errors import ReleaseUnavailable
from survey_service.wiring.errors import InferenceLoadError
from survey_service.components.link_inference import InferenceServer
from survey_service.wiring.orchestrator import Orchestrator
from survey_service.components.release_loader import ReleaseLoader
from survey_service.components.release_registry import ReleaseRegistry
from survey_service.components.session_store import MemorySessionStore

from .fakes import ACTION_RULES_VERSION, FakeReleaseReader, RecordingConsole
from .sample_bank import sample_bank

INSTALLED = {"action_rules_version": ACTION_RULES_VERSION, "model_runtime_version": model_runtime.RUNTIME_VERSION}


@pytest.fixture
def env(tmp_path):
    reader = FakeReleaseReader(tmp_path / "gate")
    reg = ReleaseRegistry()
    loader = ReleaseLoader(reader, reg, InferenceServer("fake"), workdir=tmp_path / "work", installed_versions=INSTALLED, retry_base_seconds=0)
    return reader, reg, loader


def add(reader, rid, version=1, **kw):
    reader.add_release(rid, sample_bank(version), action_rules_version=INSTALLED["action_rules_version"],
                       model_runtime_version=INSTALLED["model_runtime_version"], **kw)


def test_no_pointer_means_not_ready(env):
    reader, reg, loader = env
    assert loader.poll_once().action == "NO_POINTER" and reg.active() is None


def test_loads_active_pointer_and_skips_when_unchanged(env):
    reader, reg, loader = env
    add(reader, "rel-1")
    reader.set_pointer("rel-1")
    r = loader.poll_once()
    assert r.action == "LOADED" and reg.active().release_id == "rel-1" and reg.active().bank["version"] == 1
    assert hasattr(reg.active().session, "extract") and hasattr(reg.active().session, "respond")
    calls = dict(reader.calls)
    assert loader.poll_once().action == "UNCHANGED"
    assert reader.calls["get_release"] == calls["get_release"] and reader.calls["load_bundle"] == calls["load_bundle"]


def test_pointer_change_switches_active_new_sessions_only(env):
    reader, reg, loader = env
    add(reader, "rel-1", 1)
    add(reader, "rel-2", 2)
    reader.set_pointer("rel-1")
    loader.poll_once()
    orch = Orchestrator(MemorySessionStore(), reg, staff_console=RecordingConsole())
    o1 = orch.handle_turn(turn_id="a1", frontend_message={"session_id": "OLD", "kind": "SESSION_START"})
    assert o1.release_id == "rel-1"
    reader.set_pointer("rel-2")
    assert loader.poll_once().action == "LOADED" and reg.active_id() == "rel-2"
    o2 = orch.handle_turn(turn_id="a2", frontend_message={"session_id": "OLD", "kind": "SESSION_START"})
    o3 = orch.handle_turn(turn_id="a3", frontend_message={"session_id": "NEW", "kind": "SESSION_START"})
    assert o2.release_id == "rel-1" and o2.orchestrator_output["log_entry"]["question_bank_version"] == 1
    assert o3.release_id == "rel-2" and o3.orchestrator_output["log_entry"]["question_bank_version"] == 2


def test_unsupported_contract_version_is_rejected_and_previous_kept(env):
    reader, reg, loader = env
    add(reader, "rel-1")
    add(reader, "rel-9", runtime_contract_version=2)
    reader.set_pointer("rel-1")
    loader.poll_once()
    reader.set_pointer("rel-9")
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "UNSUPPORTED_VERSION"
    assert reg.active_id() == "rel-1" and "rel-9" not in reg.loaded_ids()
    assert "load_bundle" in reader.calls and reader.calls["load_bundle"] == 1      # 두 번째 릴리스의 번들은 읽지 않았다
    assert loader.poll_once().action == "UNCHANGED"                                  # 같은 포인터는 다시 시도하지 않는다
    assert loader.status.rejections[-1]["code"] == "UNSUPPORTED_VERSION"


@pytest.mark.parametrize("kw,code", [
    (dict(decision_result="REJECT"), "NO_APPROVAL"),
    (dict(decision_subject_swap=True), "NO_APPROVAL"),
    (dict(tamper_bank=True), "INTEGRITY_ERROR"),
])
def test_unapproved_or_tampered_bundle_rejected(env, kw, code):
    reader, reg, loader = env
    add(reader, "rel-1")
    add(reader, "rel-bad", **kw)
    reader.set_pointer("rel-1")
    loader.poll_once()
    reader.set_pointer("rel-bad")
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == code and reg.active_id() == "rel-1"


def test_missing_decision_rejected(env):
    reader, reg, loader = env
    add(reader, "rel-1")
    reader.decisions.clear()
    reader.set_pointer("rel-1")
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "NO_APPROVAL" and reg.active() is None


def test_manifest_hash_mismatch_rejected(env):
    reader, reg, loader = env
    add(reader, "rel-1")
    reader.manifests["rel-1"]["action_rules_version"] = "999"        # content_hash가 더는 맞지 않는다
    reader.set_pointer("rel-1")
    assert loader.poll_once().code == "INTEGRITY_ERROR"


def test_transient_failure_retries_same_pointer(env):
    reader, reg, loader = env
    add(reader, "rel-1")
    reader.set_pointer("rel-1")
    reader.errors["get_release"] = [BoundaryError("UNAVAILABLE", "일시 장애")]
    assert loader.poll_once().action == "RETRY_LATER" and reg.active() is None
    assert loader.poll_once().action == "LOADED" and reg.active_id() == "rel-1"
    reader.errors["get_active_pointer"] = [BoundaryError("UNAVAILABLE", "x")]
    assert loader.poll_once().action == "RETRY_LATER" and loader.status.last_poll_error == "UNAVAILABLE"
    assert reg.active_id() == "rel-1"


def test_model_runtime_refusal_keeps_previous(env):
    reader, reg, loader = env
    add(reader, "rel-1")
    reader.set_pointer("rel-1")
    loader.poll_once()
    add(reader, "rel-2")
    from boundary_contracts import canonical
    m = reader.manifests["rel-2"]
    for role in ("LORA_A", "LORA_B"):
        m["adapters"][role]["serving_config_hash"] = "sha256:" + "1" * 64      # 설정 해시가 설정과 맞지 않는다
    m["content_hash"] = canonical.content_hash({k: v for k, v in m.items() if k != "content_hash"})
    reader.set_pointer("rel-2")
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "ServingConfigMismatch" and reg.active_id() == "rel-1"


def test_different_serving_config_per_role_is_loaded(env):
    reader, reg, loader = env
    cfg = model_runtime.fake_serving_config()
    add(reader, "rel-1", serving_config_b=dict(cfg, max_attempts=2))
    reader.set_pointer("rel-1")
    assert loader.poll_once().action == "LOADED" and reg.active_id() == "rel-1"
    assert reg.active().session.serving_config_hashes["LORA_A"] != reg.active().session.serving_config_hashes["LORA_B"]


@pytest.mark.parametrize("role", ["LORA_A", "LORA_B"])
def test_single_role_hash_mismatch_is_rejected(env, role):
    reader, reg, loader = env
    add(reader, "rel-1")
    reader.set_pointer("rel-1"); loader.poll_once()
    add(reader, "rel-2", serving_config_b=dict(model_runtime.fake_serving_config(), max_attempts=2))
    from boundary_contracts import canonical
    m = reader.manifests["rel-2"]
    m["adapters"][role]["serving_config_hash"] = "sha256:" + "2" * 64
    m["content_hash"] = canonical.content_hash({k: v for k, v in m.items() if k != "content_hash"})
    reader.set_pointer("rel-2")
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "ServingConfigMismatch" and reg.active_id() == "rel-1"


def _bump(v, part):
    a, b, c = (int(x) for x in v.split(".")[:3])
    return {"major": f"{a + 1}.{b}.{c}", "minor": f"{a}.{b + 1}.{c}", "patch": f"{a}.{b}.{c + 1}"}[part]


@pytest.mark.parametrize("part", ["major", "minor"])
def test_model_runtime_major_minor_mismatch_is_rejected_and_previous_kept(env, part):
    reader, reg, loader = env
    add(reader, "rel-1")
    reader.set_pointer("rel-1"); loader.poll_once()
    reader.add_release("rel-2", sample_bank(2), action_rules_version=INSTALLED["action_rules_version"],
                       model_runtime_version=_bump(INSTALLED["model_runtime_version"], part))
    reader.set_pointer("rel-2")
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "VERSION_MISMATCH" and reg.active_id() == "rel-1"
    assert "MAJOR.MINOR" in loader.status.rejections[-1]["detail"]


def test_action_rules_version_must_equal_the_installed_contract_rules_version(env):
    """3.0.0: 계보 표지는 LoRA-A 출력 규칙 판(순번)이다. 다르면 거부하고 이전 릴리스를 유지한다. 같은 판이면 적재된다."""
    reader, reg, loader = env
    add(reader, "rel-1")
    reader.set_pointer("rel-1"); loader.poll_once()
    reader.add_release("rel-2", sample_bank(2), action_rules_version=str(int(INSTALLED["action_rules_version"]) + 1),
                       model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-2")
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "VERSION_MISMATCH" and reg.active_id() == "rel-1"
    assert "action_rules_version" in loader.status.rejections[-1]["detail"]


def test_model_runtime_patch_mismatch_only_warns(env):
    reader, reg, loader = env
    reader.add_release("rel-1", sample_bank(), action_rules_version=INSTALLED["action_rules_version"],
                       model_runtime_version=_bump(INSTALLED["model_runtime_version"], "patch"))
    reader.set_pointer("rel-1")
    assert loader.poll_once().action == "LOADED" and reg.active_id() == "rel-1"
    assert "PATCH" in loader.status.warnings[-1]["warning"] and reg.active().warnings


def test_unparsable_model_runtime_version_rejected(env):
    reader, reg, loader = env
    reader.add_release("rel-1", sample_bank(), action_rules_version=INSTALLED["action_rules_version"], model_runtime_version="latest")
    reader.set_pointer("rel-1")
    assert loader.poll_once().code == "VERSION_MISMATCH"


def test_rollback_to_loaded_release_reactivates_without_reload(env):
    reader, reg, loader = env
    add(reader, "rel-1", 1)
    add(reader, "rel-2", 2)
    reader.set_pointer("rel-1"); loader.poll_once()
    reader.set_pointer("rel-2"); loader.poll_once()
    n = reader.calls["load_bundle"]
    reader.set_pointer("rel-1", cause="ROLLBACK")
    r = loader.poll_once()
    assert r.action == "ACTIVATED" and reg.active_id() == "rel-1" and reader.calls["load_bundle"] == n


def test_gc_keeps_releases_used_by_open_sessions(env):
    reader, reg, loader = env
    loader.keep = 0
    loader.open_release_ids = lambda: {"rel-1"}
    for i in (1, 2, 3):
        add(reader, f"rel-{i}", i)
        reader.set_pointer(f"rel-{i}")
        loader.poll_once()
    assert set(reg.loaded_ids()) == {"rel-1", "rel-3"}          # rel-2는 쓰는 세션이 없어 내렸다


def test_registry_never_calls_the_reader_during_a_turn(env):
    reader, reg, loader = env
    add(reader, "rel-1", 1)
    reader.set_pointer("rel-1"); loader.poll_once()
    orch = Orchestrator(MemorySessionStore(), reg, staff_console=RecordingConsole())
    before = dict(reader.calls)
    for i in range(3):
        orch.handle_turn(turn_id=f"t{i}", frontend_message={"session_id": f"S{i}", "kind": "SESSION_START"})
    assert reader.calls == before                                    # 턴 처리 중에는 게이트(I4)를 부르지 않는다
    with pytest.raises(ReleaseUnavailable):
        reg.get("rel-unknown")


def test_restart_preloads_release_of_open_session_outside_turns(env):
    reader, reg, loader = env
    add(reader, "rel-1", 1)
    add(reader, "rel-2", 2)
    reader.set_pointer("rel-2"); loader.poll_once()
    assert loader.ensure_loaded({"rel-1", "rel-2"}) == [] and reg.get("rel-1").bank["version"] == 1
    reg._loaded.pop("rel-1"); reader.manifests.pop("rel-1")
    assert loader.ensure_loaded({"rel-1"}) == ["rel-1"] and loader.status.unavailable_releases == ["rel-1"]
    with pytest.raises(ReleaseUnavailable):
        reg.get("rel-1")


def test_inference_backend_must_be_known():
    with pytest.raises(InferenceLoadError) as e:
        InferenceServer("vllm")
    assert e.value.load_code == "UNKNOWN_BACKEND"
    assert InferenceServer("fake").production_safe is False


def test_release_expectations_from_installed_libraries(env):
    """설치된 model-runtime이 내보내는 serving_config 기준값(prompt 버전, 보호 정책)과 역할별 system_prompt를 가진 매니페스트를 적재한다."""
    reader, reg, loader = env
    import model_runtime
    from model_runtime.prompts import SUPPORTED_TEMPLATE_VERSIONS
    from survey_service.components.link_inference import role_configs
    add(reader, "rel-1")
    reader.set_pointer("rel-1")
    assert loader.poll_once().action == "LOADED"
    m = reg.active().manifest
    for role in ("LORA_A", "LORA_B"):
        cfg = m["adapters"][role]["serving_config"]
        # 기대값은 시험 대상(서비스가 적재한 매니페스트)과 독립인 model-runtime의 지원 목록이다
        assert cfg["prompt_template_version"] in SUPPORTED_TEMPLATE_VERSIONS
        assert cfg["protection_policy_version"] in model_runtime.PROTECTION_POLICIES
        model_runtime.validate_serving_config(cfg)          # 서버(model-runtime)가 받는 값이다
        assert m["adapters"][role]["system_prompt"]["path"] == "system_prompt.txt"
    # 서비스가 I9 load에 넘기는 값은 매니페스트의 역할별 값 그대로이고, 해시는 서버가 같은 설정에서 계산하는 값과 같다
    cfgs, hashes, _ = role_configs(m)
    for role in ("LORA_A", "LORA_B"):
        assert cfgs[role] == m["adapters"][role]["serving_config"]
        assert hashes[role] == model_runtime.serving_config_hash(cfgs[role])
    assert m["pipeline_schema_version"] == PIPELINE_SCHEMA_VERSION      # 설치된 계약 패키지의 값
    assert not [w for w in reg.active().warnings if "폐기" in w]


def test_release_with_prompt_3_is_rejected(env):
    """model-runtime의 지원 목록에 없는 prompt 버전은 적재되지 않는다. 이전 릴리스를 유지하고 거부한다."""
    import model_runtime
    reader, reg, loader = env
    from model_runtime.prompts import SUPPORTED_TEMPLATE_VERSIONS
    stem = "prompt"
    newest = max(int(v.rsplit("-", 1)[1]) for v in SUPPORTED_TEMPLATE_VERSIONS)
    unsupported = next(f"{stem}-{n}" for n in range(newest + 1, newest + 50) if f"{stem}-{n}" not in SUPPORTED_TEMPLATE_VERSIONS)   # 지원 목록에 없는 버전
    cfg = dict(model_runtime.fake_serving_config(), prompt_template_version=unsupported)
    reader.add_release("rel-1", sample_bank(), serving_config_b=cfg)
    reader.set_pointer("rel-1")
    r = loader.poll_once()
    assert r.action == "REJECTED"
    assert reg.active() is None or reg.active().release_id != "rel-1"


def test_release_built_with_older_model_runtime_minor_is_rejected(env):
    reader, reg, loader = env
    major, minor, patch = (int(x) for x in INSTALLED["model_runtime_version"].split(".")[:3])
    assert minor > 0
    older = f"{major}.{minor - 1}.{patch}"      # 현재 MINOR -1: 거부되어야 하는 옛 값은 현재 값에서 계산한다
    reader.add_release("rel-1", sample_bank(), action_rules_version=INSTALLED["action_rules_version"], model_runtime_version=older)
    reader.set_pointer("rel-1")
    r = loader.poll_once()
    assert r.action == "REJECTED" and r.code == "VERSION_MISMATCH" and reg.active() is None
