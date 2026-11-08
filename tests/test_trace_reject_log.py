"""#20: Trace 전송이 재시도하지 않는 실패로 끝나면 로그에 거부 사유(BoundaryError code·details.reason·message)가 남는다. 발화·비밀 값은 남지 않는다.
왜: 데모에서 수집 쪽 로그에 사유가 없어 보내는 쪽 로그의 'code=INVALID_RECORD' 뿐이라 원인을 outbox 를 열어 찾아야 했다."""
import logging

from boundary_contracts import BoundaryError

from .timing import FakeWait, fake_clock
from survey_service.components.link_trace_export import TraceExporter
from survey_service.components.trace_outbox import Outbox

SECRET = "환자-발화-원문-canary"


class RejectingIngest:
    def send(self, envelope):
        raise BoundaryError("INVALID_RECORD", "envelope와 trace의 session_id/turn_id가 다르다", {"reason": "ENVELOPE_TRACE_MISMATCH"})


def test_non_retryable_failure_logs_code_reason_and_message_without_the_utterance(tmp_path, caplog):
    clock = fake_clock()
    ob = Outbox(str(tmp_path / "ob.db"))
    ob.enqueue({"session_id": "S1", "turn_id": "t1", "trace": {"utterance": SECRET}}, clock())
    ex = TraceExporter(ob, RejectingIngest(), clock=clock, wait=FakeWait(), validate=False)
    caplog.set_level(logging.ERROR, logger="survey_service.trace")
    assert ex.flush_once().dead == 1
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "code=INVALID_RECORD" in text and "reason=ENVELOPE_TRACE_MISMATCH" in text and "session_id/turn_id가 다르다" in text and "session=S1 turn=t1" in text
    assert SECRET not in text
    ob.close()
