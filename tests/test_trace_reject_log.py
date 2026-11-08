"""#20: Trace 전송이 재시도하지 않는 실패로 끝나면 로그에 거부 사유 코드(BoundaryError code, 정의된 details.reason)가 남는다.
오류 message·reason 원문은 남지 않는다: 계약상 임의 문자열이라 받는 쪽 검증 오류가 환자 발화·비밀을 담을 수 있다.
왜: 데모에서 수집 쪽 로그에 사유가 없어 보내는 쪽 로그의 'code=INVALID_RECORD' 뿐이라 원인을 outbox 를 열어 찾아야 했다."""
import logging

import pytest
from boundary_contracts import BoundaryError, load_schema

from .timing import FakeWait, fake_clock
from survey_service.components.link_trace_export import DEFINED_REASONS, TraceExporter
from survey_service.components.trace_outbox import Outbox

UTTERANCE = "환자-발화-원문-canary"
MSG_CANARY = "message-canary-ZX81 환자가 말한 값"
REASON_CANARY = "reason-canary-QW42"


class RejectingIngest:
    def __init__(self, message, reason):
        self.message, self.reason = message, reason

    def send(self, envelope):
        raise BoundaryError("INVALID_RECORD", self.message, {"reason": self.reason} if self.reason else None)


def _flush(tmp_path, caplog, message, reason):
    clock = fake_clock()
    ob = Outbox(str(tmp_path / "ob.db"))
    ob.enqueue({"session_id": "S1", "turn_id": "t1", "trace": {"utterance": UTTERANCE}}, clock())
    ex = TraceExporter(ob, RejectingIngest(message, reason), clock=clock, wait=FakeWait(), validate=False)
    caplog.set_level(logging.ERROR, logger="survey_service.trace")
    assert ex.flush_once().dead == 1
    ob.close()
    return "\n".join(r.getMessage() for r in caplog.records)


def test_code_and_a_defined_reason_are_logged_but_never_the_message(tmp_path, caplog):
    text = _flush(tmp_path, caplog, MSG_CANARY, "OUTPUT_INVALID")
    assert "code=INVALID_RECORD" in text and "reason=OUTPUT_INVALID" in text and "session=S1 turn=t1" in text
    assert MSG_CANARY not in text and "message-canary" not in text and UTTERANCE not in text


def test_an_undefined_reason_is_logged_as_other_and_its_text_never_appears(tmp_path, caplog):
    text = _flush(tmp_path, caplog, MSG_CANARY, REASON_CANARY)
    assert "reason=OTHER" in text and REASON_CANARY not in text and MSG_CANARY not in text


def test_no_reason_is_logged_as_a_dash(tmp_path, caplog):
    assert "reason=- " in _flush(tmp_path, caplog, "x", None)


def test_defined_reason_names_match_the_contract_schema_description():
    """코드의 목록이 계약과 어긋나지 않게: BoundaryError 스키마 설명에 예시로 든 이름이 모두 이 목록에 있다(목록이 더 길 수는 있다)."""
    desc = load_schema("BoundaryError")["properties"]["details"]["properties"]["reason"]["description"]
    named = {"OUTPUT_INVALID", "EGRESS_DENIED", "TURN_IN_PROGRESS", "STALE_REPORT", "ALREADY_DECIDED", "QUESTION_SET_REQUIRED", "UNKNOWN_QUESTION_SET", "NO_OPEN_ALERT",
             "INTERVAL", "RETRY_LIMIT", "ACCOUNT_PENDING_APPROVAL", "NO_ACTIVE_RELEASE"}
    assert all(n in desc for n in named) and named <= DEFINED_REASONS


def test_reverting_to_the_raw_message_would_be_caught(tmp_path, caplog, monkeypatch):
    """변이 확인: 원문 message 를 로그에 다시 넣는 구현이면 위 시험의 canary 단언이 실패한다(그 구현을 흉내 내 같은 단언을 건다)."""
    import survey_service.components.link_trace_export as m
    real = m.log.error

    def leaky(msg, *a, **k):
        return real(msg + " message=" + MSG_CANARY, *a, **k)
    monkeypatch.setattr(m.log, "error", leaky)
    text = _flush(tmp_path, caplog, MSG_CANARY, "OUTPUT_INVALID")
    assert MSG_CANARY in text      # 누설 구현에서는 canary 가 로그에 나온다 = 위 시험의 `not in` 단언이 실패한다
