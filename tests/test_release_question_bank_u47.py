"""U47(5)·SV-16: 문항 세트만 필요하면 계약 I4 `get_release_question_bank`(2.7.0)로 받고 어댑터를 내려받지 않는다 (ADR-0040).
어댑터는 원격 추론 서버(model-runtime)가 따로 받는다. 서비스 안에서 모델을 올리는 가짜 백엔드(InferenceServer)만 어댑터를 쓰므로 그때만 load_bundle이다."""
import pytest
from boundary_contracts.clients import HttpReleaseReader

from survey_service.components.link_inference import InferenceServer
from survey_service.components.release_loader import ReleaseLoader
from survey_service.components.release_registry import ReleaseRegistry
from survey_service.components.link_inference import RemoteInference

from .timing import HANG_GUARD_SECONDS
from .fakes import FakeReleaseReader
from .http_support import TOK, publish
from .runtime_support import FlakyRuntime
from .sample_bank import sample_bank
from .test_inference_client import TOKEN, Env
from .test_release_loader import INSTALLED


def test_remote_inference_loads_the_bank_without_downloading_adapters(tmp_path):
    e = Env(tmp_path)
    try:
        assert e.loader.poll_once().action == "LOADED"
        assert e.reader.calls.get("get_release_question_bank") == 1 and e.reader.calls.get("load_bundle", 0) == 0
        assert e.registry.active().bank == sample_bank()
        assert not (tmp_path / "svc-work" / "rel-1" / "adapters").exists()           # 어댑터를 받지 않았다
    finally:
        e.close()


def test_local_fake_backend_still_needs_the_adapters_so_it_uses_load_bundle(tmp_path):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", sample_bank())
    reader.set_pointer("rel-1")
    reg = ReleaseRegistry()
    loader = ReleaseLoader(reader, reg, InferenceServer("fake"), workdir=tmp_path / "w", installed_versions=INSTALLED, retry_base_seconds=0)
    assert loader.poll_once().action == "LOADED"
    assert reader.calls.get("load_bundle") == 1 and reader.calls.get("get_release_question_bank", 0) == 0


def test_bank_that_differs_from_the_manifest_is_rejected_on_the_question_bank_path(tmp_path):
    """클라이언트는 응답의 content_hash로 본문만 검증한다. 매니페스트(question_set.content_hash)와의 결속은 서비스가 본다."""
    e = Env(tmp_path)
    try:
        e.reader.add_release("rel-2", sample_bank(2), tamper_bank=True,
                             action_rules_version=INSTALLED["action_rules_version"], model_runtime_version=INSTALLED["model_runtime_version"])
        e.reader.set_pointer("rel-2")
        r = e.loader.poll_once()
        assert r.action == "REJECTED" and r.code == "INTEGRITY_ERROR"
    finally:
        e.close()


def test_http_question_bank_endpoint_is_used_with_remote_inference(tmp_path):
    """계약 클라이언트 HttpReleaseReader + 계약 참조 서버: /question-bank만 부르고 /bundle은 부르지 않는다."""
    from conformance.servers import ReferenceServer
    rt = FlakyRuntime({TOKEN: "survey-service"}).start()
    try:
        with ReferenceServer(TOK) as srv:
            publish(srv, tmp_path, "rel-1")
            client = RemoteInference(rt.url, TOKEN, HANG_GUARD_SECONDS, load_timeout=HANG_GUARD_SECONDS, poll_interval=0.05)
            loader = ReleaseLoader(HttpReleaseReader(srv.url, "r-tok", 5), ReleaseRegistry(), client, workdir=tmp_path / "work",
                                   installed_versions=INSTALLED, retry_base_seconds=0)
            assert loader.poll_once().action == "LOADED"
            paths = [p for _, p, _ in srv.requests]
            assert any(p.endswith("/question-bank") for p in paths) and not any(p.endswith("/bundle") for p in paths)
    finally:
        rt.stop()


def test_the_fake_reader_and_the_http_client_satisfy_the_release_reader_protocol(tmp_path):
    """계약 3.0.0: sdk.ReleaseReader Protocol에 get_release_question_bank가 들어갔다. 시험용 가짜와 계약 HTTP 클라이언트가 둘 다 만족한다."""
    from boundary_contracts import sdk
    assert isinstance(FakeReleaseReader(tmp_path / "gate"), sdk.ReleaseReader)
    assert isinstance(HttpReleaseReader("http://127.0.0.1:1", "t", 1), sdk.ReleaseReader)
