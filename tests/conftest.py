import pytest


@pytest.fixture(autouse=True)
def _api_token(monkeypatch):
    """서비스 조립(App)은 공개 API 토큰을 환경 변수에서 읽는다(SURVEY_API_TOKEN)."""
    monkeypatch.setenv("SURVEY_API_TOKEN", "test-api-token-0123")
    monkeypatch.setenv("SURVEY_STAFF_API_TOKEN", "test-staff-token")


@pytest.fixture(autouse=True)
def _injected_clock_and_timer_for_pg():
    """PgConn은 시계와 시한 타이머를 주입받아야 만들어진다(ADR-0050). 시험은 가짜를 넣는다: 재접속 유예는 가짜 단조 시계(시험이 흘릴 때만 간다), 문장 강제 끊기 타이머는 fire()할 때만 실행."""
    from survey_service.components import session_store
    from .timing import FakeClock, FakeTimer
    session_store.configure(monotonic=FakeClock(), timer=FakeTimer)
    yield


@pytest.fixture(autouse=True)
def _fake_dns_for_push(monkeypatch):
    """웹 푸시 endpoint는 등록 때와 발송 때 이름을 해석해 공인 주소인지 본다(R3.18). 시험은 실제 DNS를 쓰지 않는다: 내부 이름은 사설 주소로, 그 밖의 이름은 공인 문서용 주소가 아닌 공인 주소(93.184.216.34)로 해석한다."""
    import ipaddress
    import socket
    from survey_service.components import link_push

    def fake(host, port, *a, **k):
        try:
            ipaddress.ip_address(host.split("%")[0])
            return socket.getaddrinfo(host, port, *a, **k)
        except ValueError:
            pass
        h = host.lower().rstrip(".")
        if h.endswith(".invalid"):                      # RFC 6761: 해석되지 않는 이름
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        ip = "127.0.0.1" if h == "localhost" else "10.0.0.5" if h.endswith((".internal", ".local", ".lan", ".corp", ".home", ".intranet", ".localdomain")) or h.startswith("private.") else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    monkeypatch.setattr(link_push, "_getaddrinfo", fake)


@pytest.fixture
def loopback_ok(monkeypatch):
    """웹 푸시 발송기를 로컬 푸시 서비스(127.0.0.1)로 시험하기 위한 시험 대체: 제품에는 사설 주소를 허용하는 스위치가 없다(R3.18). 127.0.0.1 리터럴만 공개로 본다."""
    from urllib.parse import urlsplit
    from survey_service.components import link_push
    orig = link_push.host_is_public
    monkeypatch.setattr(link_push, "host_is_public", lambda url: True if urlsplit(url).hostname == "127.0.0.1" else orig(url))
