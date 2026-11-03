"""코드 단계: Exposure(routes + egress + max_body_bytes)와 JobsDeclaration(잡마다 timeout_seconds)이 계약을 지키고 코드와 맞는다."""
import json
from pathlib import Path

from boundary_contracts import validator

ROOT = Path(__file__).resolve().parent.parent
EXPOSURE = json.loads((ROOT / "deploy-exposure.json").read_text(encoding="utf-8"))
JOBS = json.loads((ROOT / "deploy-jobs.json").read_text(encoding="utf-8"))


def test_egress_lists_exactly_the_neighbours_this_service_calls():
    """INTERFACES 한눈에 표: survey-service는 I4(release-gate), I6(ops-feedback 수집), I9(model-serving)를 부르고, 바깥으로는 웹 푸시와 백업만 나간다(R6.4)."""
    assert sorted(EXPOSURE["egress"]) == sorted(["release-gate", "ops-feedback-ingest", "model-serving", "external:web-push", "external:backup"])
    assert list(validator("Exposure").iter_errors(EXPOSURE)) == []


def test_every_body_taking_route_declares_a_max_body_matching_the_code():
    from survey_service.components.link_public_api import MAX_BODY
    posts = [r for r in EXPOSURE["routes"] if "POST" in (r.get("methods") or []) and r["auth"]["kind"] != "denied"]
    assert posts and all(r["max_body_bytes"] == MAX_BODY for r in posts)      # 선언과 코드(Route.max_body)가 같은 값이다


def test_every_job_has_a_timeout_and_the_backup_job_is_declared_with_its_secrets_and_env():
    assert list(validator("JobsDeclaration").iter_errors(JOBS)) == []
    jobs = {j["name"]: j for j in JOBS["jobs"]}
    assert all(isinstance(j.get("timeout_seconds"), int) for j in jobs.values())
    b = jobs["backup"]
    assert b["argv"] == ["python", "-m", "survey_service", "backup"] and b["idempotent"] is True
    assert set(b["secret_files"]) == {"SURVEY_DATABASE_URL_FILE", "OPENIT_BACKUP_CREDENTIALS_FILE", "OPENIT_BACKUP_REPO_PASSWORD_FILE"} and "OPENIT_BACKUP_URL" in b["env"]


def test_job_subcommands_exist_in_the_entry_point(capsys):
    from survey_service.server import main
    assert main(["no-such-job"]) == 2
    assert "backup" in capsys.readouterr().err
