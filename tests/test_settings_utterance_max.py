"""발화 길이 상한은 스키마(UtteranceBody.utterance.maxLength)와 설정(SURVEY_API_MAX_UTTERANCE_CHARS)이 한 곳에서 정해진다."""
import pytest

from survey_service import types
from survey_service.settings import ConfigError, Settings

BASE = {"SURVEY_RELEASE_READER": "tests.fakes:release_reader", "SURVEY_TRACE_INGEST": "tests.fakes:trace_ingest", "SURVEY_MODEL_BACKEND": "fake"}
MAX = types.schema("UtteranceBody")["properties"]["utterance"]["maxLength"]


def test_default_equals_the_schema_limit():
    assert Settings.from_env(BASE).api_max_utterance_chars == MAX == Settings().api_max_utterance_chars


def test_a_smaller_limit_is_allowed_and_a_larger_one_is_rejected():
    assert Settings.from_env({**BASE, "SURVEY_API_MAX_UTTERANCE_CHARS": "200"}).api_max_utterance_chars == 200
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, "SURVEY_API_MAX_UTTERANCE_CHARS": str(MAX + 1)})
