"""E2E 5단계의 503 원인을 볼 수 없던 결함: 로그 포매터가 예외 트레이스를 버렸다. 이제 종류 연쇄와 프레임(파일·줄·함수)을 남기고, 메시지·발화·비밀은 남기지 않는다."""
import io
import json
import logging

from boundary_contracts import BoundaryError

from survey_service.wiring.app import JsonFormatter

SECRET = "postgresql://svc:S3cretPw@db:5432/x"
UTTERANCE = "가슴이 너무 아파요 제 이름은 홍길동"


def _emit(fn):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter())
    lg = logging.getLogger("test_exc_trace")
    lg.handlers[:] = [h]
    lg.propagate = False
    try:
        fn()
    except Exception:
        lg.exception("처리 중 예기치 않은 예외 (POST /v1/sessions)")
    return json.loads(buf.getvalue())


def _boom():
    try:
        raise ValueError(f"접속 실패 {SECRET} 발화 {UTTERANCE}")
    except ValueError as e:
        raise RuntimeError("wrapped " + UTTERANCE) from e


def test_trace_is_kept_without_messages_values_or_secrets():
    rec = _emit(_boom)
    raw = json.dumps(rec, ensure_ascii=False)
    assert rec["exc"]["types"] == ["builtins.RuntimeError", "builtins.ValueError"]
    assert any(f[2] == "_boom" and f[0].endswith("test_log_exception_trace.py") for f in rec["exc"]["frames"])
    assert SECRET not in raw and "S3cretPw" not in raw and UTTERANCE not in raw and "홍길동" not in raw


def test_boundary_error_code_and_reason_are_kept():
    def f():
        raise BoundaryError("UNAVAILABLE", "내부 메시지", {"reason": "STORE_ERROR", "secret": "x"})
    rec = _emit(f)
    assert rec["exc"]["code"] == "UNAVAILABLE" and rec["exc"]["reason"] == "STORE_ERROR" and "내부 메시지" not in json.dumps(rec, ensure_ascii=False)


def test_records_without_exception_have_no_exc_field():
    buf = io.StringIO()
    h = logging.StreamHandler(buf); h.setFormatter(JsonFormatter())
    lg = logging.getLogger("test_no_exc"); lg.handlers[:] = [h]; lg.propagate = False
    lg.warning("평범한 줄")
    assert "exc" not in json.loads(buf.getvalue())
