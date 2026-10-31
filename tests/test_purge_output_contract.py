"""R7: 보존 정리 잡(`python -m survey_service purge`)의 표준 출력은 내보내기 전에 계약 SurveyPurgeOutput으로 검증한다. 정상 출력은 계약을 통과하고, 위반이면 표준 출력이 비어 있고 표준 오류와 종료 코드로 알린다."""
import json

from boundary_contracts import validator

from survey_service.wiring import purge_job

ENV = {"SURVEY_RELEASE_READER": "tests.fakes:release_reader", "SURVEY_TRACE_INGEST": "tests.fakes:trace_ingest", "SURVEY_MODEL_BACKEND": "fake"}


def _env(tmp_path):
    return {**ENV, "SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_OUTBOX_DB": str(tmp_path / "o.db"), "SURVEY_WORK_DIR": str(tmp_path / "w")}


def test_normal_output_passes_the_contract(tmp_path, capsys):
    assert purge_job.run_purge(env=_env(tmp_path)) == 0
    out = capsys.readouterr().out
    assert not list(validator("SurveyPurgeOutput").iter_errors(json.loads(out)))


def test_single_session_output_passes_the_contract(tmp_path, capsys):
    assert purge_job.run_purge(env=_env(tmp_path), argv=["--session", "S1"]) == 0
    assert not list(validator("SurveyPurgeOutput").iter_errors(json.loads(capsys.readouterr().out)))


def test_a_violating_output_is_not_printed(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(purge_job, "purge", lambda *a, **k: {"configured": True, "purged_sessions": -1})      # 계약 위반(음수)
    assert purge_job.run_purge(env=_env(tmp_path)) == 4
    cap = capsys.readouterr()
    assert cap.out == "" and "SurveyPurgeOutput" in cap.err
