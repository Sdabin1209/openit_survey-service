"""/readyz의 reask_limit.effective: 이웃(deploy E2E)이 되묻기 실효 한도 규칙을 흉내 내지 않고 이 값을 읽는다(구조 규칙 4, ADR-0046)."""
import copy
import time

from survey_service.components.reask_cap import effective_reask_cap
from tests.sample_bank import sample_bank

from .timing import wait_until
from .test_service import get, running  # noqa: F401  (픽스처 running)


def _ready(app, reader, port, bank):
    reader.add_release("rel-1", bank)
    reader.set_pointer("rel-1")
    app.start()
    wait_until(lambda: get(port, "/readyz")[0] == 200)
    return get(port, "/readyz")[1]["reask_limit"]


def test_rule():
    s = {"ask_limit": 5, "confirm_limit": 2, "unrecognized_streak_limit": 4}
    assert effective_reask_cap(3, s) == 2 and effective_reask_cap(0, s) == 2 and effective_reask_cap(1, s) == 1
    assert effective_reask_cap(3, {}) == 3 and effective_reask_cap(0, {}) == 0 and effective_reask_cap(3, None) == 3
    assert effective_reask_cap(3, {"ask_limit": 0, "confirm_limit": True}) == 3        # 1 미만과 불리언은 저작 값으로 보지 않는다


def test_readyz_has_null_effective_without_an_active_release(running):
    app, reader, ingest, port = running
    r = get(port, "/readyz")[1]["reask_limit"]
    assert r["max_consecutive"] == 3 and r["effective"] is None and r["active"] is True


def test_readyz_effective_is_the_smaller_limit_the_bank_sets(running):
    app, reader, ingest, port = running
    bank = copy.deepcopy(sample_bank())
    bank["session_settings"]["confirm_limit"] = 2
    bank["session_settings"]["ask_limit"] = 5
    bank["session_settings"]["unrecognized_streak_limit"] = 4
    r = _ready(app, reader, port, bank)
    assert r == {"active": True, "max_consecutive": 3, "effective": 2}


def test_readyz_effective_is_the_service_cap_when_the_bank_limits_are_larger(running):
    app, reader, ingest, port = running
    bank = copy.deepcopy(sample_bank())
    for k in ("ask_limit", "confirm_limit", "unrecognized_streak_limit"):
        bank["session_settings"][k] = 9
    assert _ready(app, reader, port, bank) == {"active": True, "max_consecutive": 3, "effective": 3}


def test_readyz_matches_the_contract_schema(running):
    """계약 SurveyReadyz.reask_limit.effective는 1 이상 또는 null이다. 문항 세트는 세 상한을 반드시 저작하므로(QuestionBank 3.0.0) 활성 릴리스가 있으면 1 이상이다."""
    from boundary_contracts import validator
    app, reader, ingest, port = running
    _ready(app, reader, port, copy.deepcopy(sample_bank()))
    body = get(port, "/readyz")[1]
    assert body["reask_limit"]["effective"] >= 1
    assert not list(validator("SurveyReadyz").iter_errors(body))
