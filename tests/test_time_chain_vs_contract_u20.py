"""U20: 모델 호출 클라이언트 읽기 상한(extract 21, respond 26)은 서버 마감(20, 25)보다 크고, 서버 마감의 합은 턴 총 마감(50)보다 작다.

기준은 계약 패키지의 스키마 기본값이다(CONVENTIONS "시간 상한 사슬" 표와 같은 값, D125): 클라이언트 값은 SurveyServiceEnv, 서버 값은 ModelServingEnv.
서비스 기본값(settings.py)이 계약 기본값과 같은지도 같이 본다: 한쪽만 바뀌어 사슬이 깨지는 것을 막는다.
"""
from boundary_contracts import load_schema

from survey_service.settings import Settings


def _default(schema: str, name: str) -> float:
    return float(load_schema(schema)["properties"][name]["default"])


def _settings() -> Settings:
    return Settings.from_env({"SURVEY_RELEASE_READER": "http", "SURVEY_RELEASE_READER_URL": "http://x", "SURVEY_TRACE_INGEST": "http",
                              "SURVEY_TRACE_INGEST_URL": "http://x", "SURVEY_MODEL_BACKEND": "m:f"})


def test_client_read_timeouts_exceed_the_server_deadlines_of_the_contract():
    s = _settings()
    assert s.inference_extract_timeout_seconds > _default("ModelServingEnv", "MODEL_RUNTIME_EXTRACT_DEADLINE_S")
    assert s.inference_respond_timeout_seconds > _default("ModelServingEnv", "MODEL_RUNTIME_RESPOND_DEADLINE_S")


def test_server_deadlines_sum_is_below_the_turn_deadline():
    s = _settings()
    assert _default("ModelServingEnv", "MODEL_RUNTIME_EXTRACT_DEADLINE_S") + _default("ModelServingEnv", "MODEL_RUNTIME_RESPOND_DEADLINE_S") < s.turn_deadline_seconds


def test_service_defaults_equal_the_contract_defaults():
    s = _settings()
    assert s.inference_extract_timeout_seconds == _default("SurveyServiceEnv", "SURVEY_INFERENCE_EXTRACT_TIMEOUT_SECONDS")
    assert s.inference_respond_timeout_seconds == _default("SurveyServiceEnv", "SURVEY_INFERENCE_RESPOND_TIMEOUT_SECONDS")
    assert s.turn_deadline_seconds == _default("SurveyServiceEnv", "SURVEY_TURN_DEADLINE_SECONDS")
