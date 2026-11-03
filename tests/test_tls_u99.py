"""U99: 이 서비스는 TLS 서버가 되지 않고(사이드카가 끝는다), 다른 서비스를 부르는 주소는 전부 설정에서 온다. 바깥으로 나가는 HTTPS는 표준 환경 변수(SSL_CERT_FILE)만으로 사설 루트를 믿고,
인증서 검증을 끄는 코드가 없으며, DB 접속 문자열의 sslmode는 코드가 덮어쓰지 않는다."""
import datetime
import http.server
import ipaddress
import re
import ssl
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(p for p in (ROOT / "survey_service").rglob("*.py") if "types" not in p.relative_to(ROOT / "survey_service").parts and "migrations" not in p.relative_to(ROOT / "survey_service").parts)


def test_no_fixed_service_addresses_in_the_source():
    """다른 서비스를 부르는 주소는 설정(환경 변수)으로만 온다: 소스에 http:// 주소나 고정 호스트 이름이 없다. (0.0.0.0은 듣는 주소이지 부르는 주소가 아니다.)"""
    # 부르는 주소가 아닌 두 곳만 예외다: 푸시 endpoint 문법 검사용 자리표시 값(link_public_api)과 SSRF 방어의 내부 이름 거부 목록(link_push)
    allowed = {("components/link_public_api.py", 'https://push.example/'), ("components/link_push.py", 'host == "localhost"')}
    bad = []
    for p in SOURCES:
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"http://|https://[A-Za-z0-9]", code) or re.search(r"""["'](localhost|127\.0\.0\.1)[:"'/]""", code):
                if not any(str(p).endswith(f) and frag in code for f, frag in allowed):
                    bad.append(f"{p.relative_to(ROOT)}:{n}")
    assert bad == [], bad


def test_service_url_settings_have_no_default_address():
    from survey_service.settings import Settings
    s = Settings.from_env({"SURVEY_MODEL_BACKEND": "fake", "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y"})
    assert s.release_reader_url == "" and s.trace_ingest_url == "" and s.inference_url == ""


def test_no_code_turns_off_certificate_verification():
    pat = re.compile(r"verify\s*=\s*False|CERT_NONE|check_hostname\s*=\s*False|_create_unverified_context|PYTHONHTTPSVERIFY|InsecureRequestWarning|disable_warnings")
    bad = [f"{p.relative_to(ROOT)}:{n}" for p in SOURCES for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pat.search(line.split("#", 1)[0])]
    assert bad == [], bad


@pytest.mark.parametrize("sslmode", ["require", "verify-full"])
def test_database_url_sslmode_is_passed_through(sslmode):
    from survey_service.components.session_store import PgConn
    url = f"postgresql://u@db.example:5432/d?sslmode={sslmode}&sslrootcert=/run/secrets/root.crt"
    params = PgConn._params(SimpleNamespace(_url=url), 3, 1.0)
    assert params["sslmode"] == sslmode and params["sslrootcert"] == "/run/secrets/root.crt" and params["connect_timeout"] == 3


def _pki(tmp_path):
    def key():
        return ec.generate_private_key(ec.SECP256R1())

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    rk, sk = key(), key()
    root = (x509.CertificateBuilder().subject_name(name("test-root")).issuer_name(name("test-root")).public_key(rk.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(rk, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(name("localhost")).issuer_name(root.subject).public_key(sk.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False).sign(rk, hashes.SHA256()))
    pem = lambda o: o.public_bytes(serialization.Encoding.PEM)
    (tmp_path / "root.pem").write_bytes(pem(root))
    (tmp_path / "leaf.pem").write_bytes(pem(leaf))
    (tmp_path / "leaf.key").write_bytes(sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return tmp_path / "root.pem", tmp_path / "leaf.pem", tmp_path / "leaf.key"


@pytest.fixture
def tls_server(tmp_path):
    root, leaf, key = _pki(tmp_path)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(leaf), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{srv.server_address[1]}", root
    srv.shutdown(); srv.server_close()


def test_https_call_trusts_a_private_root_through_ssl_cert_file_only(tls_server, monkeypatch):
    """서비스가 추론 서버를 부르는 HTTPS(RemoteInference.ready)가 SSL_CERT_FILE 하나로 사설 루트를 믿는다. 환경 변수가 없으면 믿지 않는다(검증이 켜져 있다는 증거)."""
    from survey_service.components.link_inference import RemoteInference
    url, root = tls_server
    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"):
        monkeypatch.delenv(var, raising=False)
    mk = lambda: RemoteInference(url, "tok", 3.0, monotonic=lambda: 0.0, sleep=lambda s: None)
    assert mk().ready() is False                                     # 사설 루트를 모른다: 검증 실패
    monkeypatch.setenv("SSL_CERT_FILE", str(root))
    assert mk().ready() is True


def test_contract_http_client_trusts_a_private_root_through_ssl_cert_file_only(tls_server, monkeypatch):
    """영역 사이 호출(계약 HttpReleaseReader, 이웃 release-gate의 I4)도 같다."""
    from boundary_contracts import BoundaryError
    from boundary_contracts.clients import HttpReleaseReader
    url, root = tls_server
    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(BoundaryError) as e:
        HttpReleaseReader(url, "tok", 3).get_active_pointer()
    assert e.value.code == "UNAVAILABLE"                              # 인증서 검증 실패 = 연결 실패
    monkeypatch.setenv("SSL_CERT_FILE", str(root))
    try:
        HttpReleaseReader(url, "tok", 3).get_active_pointer()
    except BoundaryError as e2:                                       # 서버가 빈 200만 준다: TLS는 통과하고 본문 해석에서 거부된다. 연결 실패(UNAVAILABLE+SSL)가 아니면 된다
        assert "CERTIFICATE_VERIFY_FAILED" not in str(e2.details) + e2.message


def test_plain_http_outside_loopback_is_refused_for_every_call_path():
    """loopback 밖 http://는 추론 서버 주소로 받지 않는다: 직접 urllib을 쓰는 /readyz 확인(RemoteInference.ready)도 같은 규칙(require_tls_url)을 거친다."""
    from survey_service.components.link_inference import RemoteInference
    mk = lambda u: RemoteInference(u, "tok", 3.0, monotonic=lambda: 0.0, sleep=lambda s: None)
    for bad in ("http://inference:8080", "http://10.0.0.5:8080", "http://infer.example.com"):
        with pytest.raises(ValueError):
            mk(bad)
    for ok in ("https://inference:8443", "http://127.0.0.1:8080", "http://localhost:8080"):
        assert mk(ok) is not None
