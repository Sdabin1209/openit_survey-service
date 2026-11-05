"""R7(내는 쪽 검증): 문진 스키마로 정한 공개 API 응답은 내보내기 전에 검증한다. 어기면 503 UNAVAILABLE 이다(link_public_api._check_survey_response)."""
import json
import pathlib

import pytest

from survey_service.components import link_public_api as m

EX = pathlib.Path(__file__).resolve().parents[1] / "schema/schemas/examples"


def _valid(name):
    f = sorted(EX.glob(f"{name}*.json"))
    f = [x for x in f if "invalid" not in x.name]
    if not f:
        pytest.skip(f"{name} 예시가 없다")
    return json.loads(f[0].read_text(encoding="utf-8"))


@pytest.mark.parametrize("route,schema", sorted(m._SURVEY_RESPONSE.items()))
def test_valid_example_passes(route, schema):
    m._check_survey_response(route, 200, _valid(schema))


@pytest.mark.parametrize("route", sorted(m._SURVEY_RESPONSE))
def test_out_of_schema_body_is_refused(route):
    # 변이: 문진 스키마 밖 본문은 내보내지 않는다
    with pytest.raises(m.HttpError) as e:
        m._check_survey_response(route, 200, {"not_a_field": 1})
    assert "UNAVAILABLE" in repr(e.value.args) or getattr(e.value, "error", None) is not None


def test_errors_and_unmapped_routes_are_not_checked():
    m._check_survey_response("turn", 400, {"code": "INVALID_RECORD"})
    m._check_survey_response("push_config", 200, {"anything": 1})
