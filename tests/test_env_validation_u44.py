"""U44: 설정(환경 변수)을 손으로만 읽지 않고 계약 SurveyServiceEnv 스키마로 검증하며, 모르는 SURVEY_* 이름은 시작 때 거부한다 (ADR-0038).
오타 설정(SURVEY_MAX_CONNECTION)이 조용히 기본값으로 도는 것을 막는다. 오류 문구에는 이름만 있고 값은 없다(값이 비밀일 수 있다)."""
import glob
import re
from pathlib import Path

import pytest

from survey_service.server import main
from survey_service.settings import ConfigError, Settings

BASE = {"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake"}
ROOT = Path(__file__).resolve().parents[1]


def test_known_configuration_is_accepted_and_non_survey_names_are_ignored():
    s = Settings.from_env({**BASE, "SURVEY_PORT": "9090", "SURVEY_LOG_LEVEL": "DEBUG", "PATH": "/usr/bin", "HOME": "/home/x"})
    assert s.port == 9090 and s.log_level == "DEBUG"


@pytest.mark.parametrize("name", ["SURVEY_MAX_CONNECTION", "SURVEY_SOCKET_TIMEOUT", "SURVEY_API_TOKEN_FIL", "SURVEY_FOO"])
def test_unknown_survey_name_is_rejected_by_name_without_the_value(name):
    with pytest.raises(ConfigError) as e:
        Settings.from_env({**BASE, name: "비밀값-secret-123"})
    assert name in str(e.value) and "비밀값-secret-123" not in str(e.value)


def test_every_unknown_name_is_reported_not_only_the_first():
    with pytest.raises(ConfigError) as e:
        Settings.from_env({**BASE, "SURVEY_AAA": "1", "SURVEY_BBB": "2"})
    assert "SURVEY_AAA" in str(e.value) and "SURVEY_BBB" in str(e.value)


@pytest.mark.parametrize("name,value", [("SURVEY_PORT", "abc"), ("SURVEY_LOG_LEVEL", "verbose"), ("SURVEY_API_TOKEN_FILE", "relative/path")])
def test_values_that_break_the_contract_format_are_rejected_by_name(name, value):
    with pytest.raises(ConfigError) as e:
        Settings.from_env({**BASE, name: value})
    assert name in str(e.value)


def test_required_names_come_from_the_contract():
    for missing in ("SURVEY_RELEASE_READER", "SURVEY_TRACE_INGEST"):     # 추론 연결은 둘 중 하나라 아래 별도 시험
        env = {k: v for k, v in BASE.items() if k != missing}
        with pytest.raises(ConfigError) as e:
            Settings.from_env(env)
        assert missing in str(e.value)


def test_every_survey_name_the_code_reads_is_accepted_by_the_contract_schema():
    """코드가 읽는 SURVEY_* 이름이 계약 목록에 없으면(또는 *_FILE로 읽는 비밀이면 그 이름이) 시작이 거부된다. 어긋나면 CR(계약에 이름 추가)."""
    names = set()
    for f in glob.glob(str(ROOT / "survey_service/**/*.py"), recursive=True):
        names |= set(re.findall(r'["\'](SURVEY_[A-Z0-9_]+)["\']', Path(f).read_text(encoding="utf-8")))
    names.discard("SURVEY_STOPPED")            # 고정 문장 키(환경 변수가 아니다)
    for n in sorted(names):
        try:
            Settings.from_env({**BASE, n: "1"})
        except ConfigError as e:
            assert n not in str(e) or "모르는" not in str(e), f"{n}: {e}"


def test_startup_exits_with_a_configuration_error_for_an_unknown_name(monkeypatch, capsys):
    for k, v in {**BASE, "SURVEY_MAX_CONNECTION": "5"}.items():
        monkeypatch.setenv(k, v)
    assert main([]) == 2
    assert "SURVEY_MAX_CONNECTION" in capsys.readouterr().err


# 계약 3.0.2: 추론 연결은 SURVEY_INFERENCE_URL(원격 추론) 또는 SURVEY_MODEL_BACKEND(서비스 안 가짜 백엔드) 중 하나면 된다. 운영 배포는 원격 추론만 준다 (D62).
REMOTE_ONLY = {"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_INFERENCE_URL": "http://inference:8081"}


def test_remote_inference_alone_is_enough_to_start():
    s = Settings.from_env(REMOTE_ONLY)
    assert s.inference_url == "http://inference:8081" and s.model_backend == ""


def test_model_backend_alone_is_still_enough():
    assert Settings.from_env({"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake"}).model_backend == "fake"


def test_neither_inference_url_nor_model_backend_is_rejected_by_name():
    with pytest.raises(ConfigError) as e:
        Settings.from_env({"SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y"})
    assert "SURVEY_INFERENCE_URL" in str(e.value) and "SURVEY_MODEL_BACKEND" in str(e.value)


def test_empty_values_do_not_count():
    with pytest.raises(ConfigError):
        Settings.from_env({**REMOTE_ONLY, "SURVEY_INFERENCE_URL": ""})


def test_startup_succeeds_to_the_app_with_remote_inference_only(monkeypatch, tmp_path):
    """시작 경로(main)가 설정 단계에서 막히지 않는다: 원격 추론만 준 구성이 설정 오류(종료 코드 2)를 내지 않는다."""
    for k, v in {**REMOTE_ONLY, "SURVEY_SESSION_DB": str(tmp_path / "s.db"), "SURVEY_WORK_DIR": str(tmp_path / "w")}.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert s.inference_url
