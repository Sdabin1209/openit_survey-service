"""FAULT_MATRIX A6(Trace outbox -> 수집: 멈춤·5xx·깨진 응답, 큐 상한), A8(릴리스 사본 볼륨: 쓰기 불가), A10(직원 SSE: 이어 받기·DB 오류·연결 누수)."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from boundary_contracts.clients import HttpTraceIngest

from survey_service.wiring.errors import StoreError
from survey_service.components.link_inference import InferenceServer
from survey_service.components.release_loader import ReleaseLoader
from survey_service.components.release_registry import ReleaseRegistry
from survey_service.components.trace_outbox import Outbox
from survey_service.components.trace_envelope import OutboxTraceSink
from survey_service.components.link_trace_export import TraceExporter

from .http_server_support import server_port, stop_server
from .fakes import FakeReleaseReader
from .sample_bank import sample_bank
from .test_release_loader import INSTALLED
from .test_staff_alerts import Sse, http_call, make_app, start_and_risk
from .test_trace_export import run_turns


# ---------------------------------------------------------------- A6
class StubIngest:
    """수집 서버 대역. mode: hang(응답 없음), 500, garbage(200인데 JSON 아님), html503(503인데 BoundaryError 본문 아님)."""
    def __init__(self, mode):
        self.mode, self.calls = mode, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.calls += 1
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if outer.mode == "hang":
                    time.sleep(3)
                    return
                code, body = {"500": (500, b"boom"), "garbage": (200, b"<html>proxy error</html>"), "html503": (503, b"<html>bad gateway</html>")}[outer.mode]
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub(request):
    made = []

    def make(mode):
        s = StubIngest(mode)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


@pytest.mark.parametrize("mode", ["hang", "500", "garbage", "html503"])
def test_ingest_stuck_or_broken_keeps_rows_pending_and_never_fails_turns(stub, tmp_path, mode):
    """F2 멈춤 / F5 5xx / F6 깨진 응답: 보낸 것으로 치지 않고(PENDING 유지, 유실 없음) 턴은 영향이 없다."""
    s = stub(mode)
    ob = Outbox(str(tmp_path / "ob.db"))
    ex = TraceExporter(ob, HttpTraceIngest(s.url, "w-tok", 0.5), base_delay=1.0, clock=lambda: 1000.0)
    t = time.monotonic()
    run_turns(OutboxTraceSink(ob, lambda: 1000.0))             # 전송은 턴 경로 밖이다
    assert time.monotonic() - t < 5 and ob.counts()["PENDING"] == 3
    res = ex.flush_once()
    assert ob.counts() == {"PENDING": 3, "SENT": 0, "DEAD": 0} and (res.retried + res.paused) >= 1
    st = ex.stats()
    assert st["sent"] == 0 and st["dead"] == 0 and st["last_error"] == "UNAVAILABLE"
    ob.close()


def test_integrity_error_is_quarantined_not_retried(tmp_path):
    from boundary_contracts import BoundaryError

    class Broken:
        calls = 0

        def send(self, env):
            Broken.calls += 1
            raise BoundaryError("INTEGRITY_ERROR", "응답이 스키마를 위반한다")
    clock = [1000.0]
    ob = Outbox(str(tmp_path / "ob.db"))
    ex = TraceExporter(ob, Broken(), base_delay=1.0, max_delay=2.0, clock=lambda: clock[0])
    run_turns(OutboxTraceSink(ob, lambda: 1000.0), n=2)
    for _ in range(12):
        ex.flush_once()
        clock[0] += 5
    assert Broken.calls == 2        # 계약 I6: UNAVAILABLE일 때만 재시도. 건당 한 번만 보냈다
    assert ob.counts() == {"PENDING": 0, "SENT": 0, "DEAD": 2} and ex.stats()["dead_by_code"] == {"INTEGRITY_ERROR": 2}
    ob.close()


def test_outbox_has_a_pending_cap_and_counts_drops(tmp_path):
    ob = Outbox(str(tmp_path / "ob.db"))
    ob.max_pending = 5
    sink = OutboxTraceSink(ob, lambda: 1000.0)
    run_turns(sink)                                            # 3건
    run_turns(sink)                                            # 같은 (세션, 턴)이라 중복(넣지 않음)
    for i in range(10):
        env = {"session_id": f"s{i}", "turn_id": "t", "x": 1}
        try:
            ob.enqueue(env, 1000.0)
        except StoreError:
            pass
    assert ob.counts()["PENDING"] <= 6 and ob.dropped_full >= 4          # 상한 근처에서 멈추고 버린 건수를 센다
    ex = TraceExporter(ob, object())
    assert ex.stats()["dropped_full"] == ob.dropped_full
    ob.close()


# ---------------------------------------------------------------- A8
def _loader(tmp_path):
    reader = FakeReleaseReader(tmp_path / "gate")
    reader.add_release("rel-1", sample_bank(), action_rules_version=INSTALLED["action_rules_version"], model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    reg = ReleaseRegistry()
    return reader, reg, ReleaseLoader(reader, reg, InferenceServer("fake"), workdir=tmp_path / "work", installed_versions=INSTALLED, retry_base_seconds=0)


def test_volume_not_writable_is_a_visible_retry_not_a_dead_poller_then_recovers(tmp_path):
    """F1 볼륨 쓰기 불가: 예외가 폴링 스레드로 새지 않고 재시도 상태(STORAGE_UNAVAILABLE)로 드러난다. 이전 릴리스는 유지, 쓰기가 되면 적재된다."""
    reader, reg, loader = _loader(tmp_path)
    real = reader.load_bundle
    reader.load_bundle = lambda rid, dest: (_ for _ in ()).throw(PermissionError("read-only"))
    r = loader.poll_once()
    assert r.action == "RETRY_LATER" and r.code == "STORAGE_UNAVAILABLE" and reg.active_id() is None
    info = loader.retry_info()
    assert info["release_id"] == "rel-1" and "STORAGE_UNAVAILABLE" in info["last_error"] and "read-only" not in json.dumps(info)
    reader.load_bundle = real
    assert loader.poll_once().action == "LOADED" and reg.active_id() == "rel-1" and loader.retry_info() is None


def test_corrupted_local_copy_is_not_trusted_it_is_replaced(tmp_path):
    """F6 사본 손상: 로컬 사본은 읽어 쓰지 않는다(매번 원본에서 받아 해시 검증). 손상된 폴더가 있어도 적재되고 문항 세트는 원본 그대로다."""
    reader, reg, loader = _loader(tmp_path)
    junk = tmp_path / "work" / "rel-1"
    junk.mkdir(parents=True)
    (junk / "question_bank.json").write_text("{깨진 JSON", encoding="utf-8")
    (junk / "manifest.json").write_text("\x00\x00", encoding="utf-8")
    assert loader.poll_once().action == "LOADED" and reg.active().bank == sample_bank()


# ---------------------------------------------------------------- A10
def test_sse_tells_client_to_resync_when_events_were_trimmed(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        for i in range(3):
            assert start_and_risk(port, f"K{i}", f"t-{i}")[0] == 200
        assert app.alert_store.trim_events(keep=1) >= 1
        sse = Sse(port, last_id=0)                              # 지워진 구간을 건너뛴 재연결
        ev = sse.events.get(timeout=5)
        assert ev["event"] == "resync" and json.loads(ev["data"])["reason"] == "EVENTS_NOT_RESUMABLE"
        sse.close()
        code, body = http_call(port, "GET", "/v1/staff/alerts?status=OPEN")           # 목록 API로 따라잡는다
        assert code == 200 and len(body["items"]) == 3
        sse2 = Sse(port, last_id=app.alert_store.latest_event_id())      # 이어 받을 수 있으면 resync는 없다
        time.sleep(0.5)
        assert sse2.events.empty()
        sse2.close()
    finally:
        app.draining.set(); stop_server(httpd); app.stop()


def test_sse_returns_503_when_alert_db_is_down_and_stream_ends_cleanly(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch)
    try:
        real = app.alert_store.latest_event_id

        def down():
            raise StoreError("알림 DB가 죽었다")
        app.alert_store.latest_event_id = down
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/staff/alerts/stream", headers={"Authorization": "Bearer test-staff-token"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 503 and e.value.headers.get("Retry-After") and json.loads(e.value.read())["code"] == "UNAVAILABLE"
        assert app.alerts.hub.count() == 0                      # 구독이 남지 않는다
        app.alert_store.latest_event_id = real
    finally:
        app.draining.set(); stop_server(httpd); app.stop()


def test_dropped_sse_clients_do_not_leak_subscriptions_or_threads(tmp_path, monkeypatch):
    app, httpd, port = make_app(tmp_path, monkeypatch, hb="0.2")
    try:
        base = threading.active_count()
        clients = [Sse(port) for _ in range(8)]
        time.sleep(0.5)
        assert app.alerts.hub.count() == 8
        for c in clients:
            c.close()                                           # 클라이언트가 말없이 떠난다
        end = time.monotonic() + 5
        while time.monotonic() < end and (app.alerts.hub.count() or threading.active_count() > base + 1):
            time.sleep(0.1)
        assert app.alerts.hub.count() == 0 and threading.active_count() <= base + 1
    finally:
        app.draining.set(); stop_server(httpd); app.stop()


def test_named_heartbeat_event_keeps_flowing_when_alert_db_dies_after_connect(tmp_path, monkeypatch):
    """브라우저 EventSource는 주석 줄을 볼 수 없다: 이름 있는 heartbeat(id 없음)를 보낸다. 스트림 스레드는 DB를 읽지 않아 DB 장애·락 점유에도 유지된다."""
    app, httpd, port = make_app(tmp_path, monkeypatch, hb="0.3")
    try:
        sse = Sse(port)
        end = time.monotonic() + 3
        while not sse.heartbeats and time.monotonic() < end:
            time.sleep(0.05)
        first = sse.heartbeats[0]
        assert "ts" in json.loads(first["data"]) and "id" not in first          # 연결 직후 한 번, id가 없어 Last-Event-ID를 바꾸지 않는다

        def down(*a, **k):
            raise StoreError("알림 DB가 죽었다")
        for name in ("latest_event_id", "oldest_event_id", "events_after", "list"):
            setattr(app.alert_store, name, down)
        n0 = len(sse.heartbeats)
        with app.alert_store._lock:                           # DB 연결의 락을 다른 쪽이 쥐고 있어도
            end = time.monotonic() + 4
            while len(sse.heartbeats) < n0 + 3 and time.monotonic() < end:
                time.sleep(0.05)
        assert len(sse.heartbeats) >= n0 + 3 and any("ping" in c for c in sse.comments)      # 기존 주석 줄도 유지
        sse.close()
    finally:
        app.draining.set(); stop_server(httpd); app.stop()
