"""RUNTIME R5.1·R10.11: /healthz·/readyz 본문은 HealthStatus를 지키고 '아님'은 503이며 not_ready 표기를 쓴다. /healthz·/readyz·/metrics 는 인증이 없다(토큰이 있어도 무시, 401·403 없음)."""
import urllib.request

from boundary_contracts import validator

from .test_service import get, running, settings  # noqa: F401,F811

HS = validator("HealthStatus")


def test_healthz_and_readyz_bodies_follow_health_status_in_ready_and_not_ready_states(running):
    app, reader, ingest, port = running
    st, body = get(port, "/healthz")
    assert st == 200 and list(HS.iter_errors(body)) == []
    st, body = get(port, "/readyz")                       # 활성 릴리스가 아직 없다: 준비되지 않음
    assert st == 503 and list(HS.iter_errors(body)) == [] and body.get("status", "not_ready") == "not_ready" and body["ready"] is False
    app.draining.set()
    st, body = get(port, "/readyz")
    assert st == 503 and list(HS.iter_errors(body)) == [] and "shutting_down" in body["problems"]


def test_probes_and_metrics_need_no_authentication_and_ignore_tokens(running):
    app, reader, ingest, port = running
    for path in ("/healthz", "/readyz", "/metrics"):
        for hdr in ({}, {"Authorization": "Bearer wrong-token-0123456789"}):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=hdr)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
            assert code in (200, 503) and code not in (401, 403), (path, hdr, code)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as r:
        assert r.status == 200 and b"openit_turn_duration_seconds" in r.read()
