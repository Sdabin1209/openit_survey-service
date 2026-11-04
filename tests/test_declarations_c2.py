"""코드 단계: Exposure(routes + egress + max_body_bytes)와 JobsDeclaration(잡마다 timeout_seconds)이 계약을 지키고 코드와 맞는다."""
import json
from pathlib import Path

from boundary_contracts import validator

ROOT = Path(__file__).resolve().parent.parent
EXPOSURE = json.loads((ROOT / "deploy-exposure.json").read_text(encoding="utf-8"))
JOBS = json.loads((ROOT / "deploy-jobs.json").read_text(encoding="utf-8"))


def test_egress_lists_exactly_the_neighbours_this_service_calls():
    """INTERFACES 한눈에 표: survey-service는 I4(release-gate), I6(ops-feedback 수집), I9(model-serving)를 부르고, 바깥으로는 웹 푸시와 백업만 나간다(R6.4)."""
    assert sorted(EXPOSURE["egress"]) == sorted(["release-gate", "ops-feedback", "model-serving", "external:web-push", "external:backup"])
    assert list(validator("Exposure").iter_errors(EXPOSURE)) == []


def test_every_body_taking_route_declares_a_max_body_matching_the_code():
    from survey_service.components.link_public_api import MAX_BODY
    posts = [r for r in EXPOSURE["routes"] if "POST" in (r.get("methods") or []) and r["auth"]["kind"] != "denied"]
    assert posts and all(r["max_body_bytes"] == MAX_BODY for r in posts)      # 선언과 코드(Route.max_body)가 같은 값이다


def test_every_job_has_a_timeout_and_the_backup_job_is_declared_with_its_secrets_and_env():
    assert list(validator("JobsDeclaration").iter_errors(JOBS)) == []
    jobs = {j["name"]: j for j in JOBS["jobs"]}
    assert all(isinstance(j.get("timeout_seconds"), int) for j in jobs.values())
    r = jobs["restore"]      # 복구 잡: 주기 없이 선언만(deploy가 사람·복구 절차가 띄운다)
    assert r["argv"] == ["python", "-m", "survey_service", "restore"] and r["idempotent"] is True and set(r["secret_files"]) == {"SURVEY_DATABASE_URL_FILE", "OPENIT_BACKUP_CREDENTIALS_FILE", "OPENIT_BACKUP_REPO_PASSWORD_FILE"}
    b = jobs["backup"]
    assert b["argv"] == ["python", "-m", "survey_service", "backup"] and b["idempotent"] is True
    assert set(b["secret_files"]) == {"SURVEY_DATABASE_URL_FILE", "OPENIT_BACKUP_CREDENTIALS_FILE", "OPENIT_BACKUP_REPO_PASSWORD_FILE"} and "OPENIT_BACKUP_URL" in b["env"]


def test_job_subcommands_exist_in_the_entry_point(capsys):
    from survey_service.server import main
    assert main(["no-such-job"]) == 2
    assert "backup" in capsys.readouterr().err


def test_every_neighbour_client_is_built_with_the_egress_proxy_from_the_environment(monkeypatch, tmp_path):
    """CONVENTIONS 13절: 계약 클라이언트를 만드는 모든 곳에서 egress_proxy_from(environ)을 넘긴다(I4·I6·I9). 환경에 출구가 있으면 세 클라이언트 모두 그 터널을 쓴다."""
    from survey_service.components.link_boundary_clients import http_release_reader, http_trace_ingest
    from survey_service.components.link_inference import RemoteInference
    proxy = "http://127.0.0.1:3128"
    env = {"OPENIT_EGRESS_PROXY": proxy, "SURVEY_RELEASE_READER_URL": "https://release-gate", "SURVEY_RELEASE_READER_TOKEN": "t-0123456789abcdef",
           "SURVEY_TRACE_INGEST_URL": "https://ops-feedback", "SURVEY_TRACE_INGEST_TOKEN": "t-0123456789abcdef"}
    import urllib.request

    def tunnel(client):      # 계약 클라이언트의 열린 연결기에서 CONNECT 출구를 읽는다
        return next((h.proxies for h in client._opener.handlers if isinstance(h, urllib.request.ProxyHandler)), {}).get("https")
    rr, ti = http_release_reader(env), http_trace_ingest(env)
    assert tunnel(rr._reader) == proxy and tunnel(rr._bundle) == proxy and tunnel(ti) == proxy
    ri = RemoteInference("https://model-serving", "tok", 3.0, monotonic=lambda: 0.0, sleep=lambda s: None, egress_proxy=proxy)
    assert tunnel(ri._rt) == proxy and all(tunnel(rt) == proxy for rt in ri._op_rts.values())
    env.pop("OPENIT_EGRESS_PROXY")
    assert tunnel(http_trace_ingest(env)) is None


def test_app_wiring_passes_the_egress_proxy_to_the_inference_client_and_the_web_push_sender(tmp_path, monkeypatch):
    """배선(App)이 환경의 OPENIT_EGRESS_PROXY를 추론 클라이언트와 웹 푸시 발송기에 넘긴다(R1.8, R6.4)."""
    import urllib.request
    from survey_service.settings import Settings
    from survey_service.wiring.app import App
    from .fakes import FakeReleaseReader, FakeTraceIngest
    from .test_staff_alerts import _vapid_key_pem
    proxy = "http://127.0.0.1:3128"
    monkeypatch.setenv("OPENIT_EGRESS_PROXY", proxy)
    monkeypatch.setenv("SURVEY_INFERENCE_TOKEN", "inf-token-0123456789")
    monkeypatch.setenv("SURVEY_VAPID_PRIVATE_KEY", _vapid_key_pem())
    monkeypatch.delenv("SURVEY_DATABASE_URL", raising=False)
    s = Settings.from_env({"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_INFERENCE_URL": "https://model-serving", "SURVEY_WORK_DIR": str(tmp_path / "w"),
                           "SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_VAPID_SUBJECT": "mailto:ops@example.com"})
    app = App(s, reader=FakeReleaseReader(tmp_path / "g"), ingest=FakeTraceIngest())
    try:
        rt = app.inference._rt
        assert next((h.proxies for h in rt._opener.handlers if isinstance(h, urllib.request.ProxyHandler)), {}).get("https") == proxy
        assert app.alerts.push._egress == proxy
    finally:
        app.stop()
