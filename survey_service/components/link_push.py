"""직원 기기 웹 푸시 전송(보내는 쪽). 입력 PushDelivery(구독 + StaffPushPayload 본문) -> 출력 PushOutcome(ACCEPTED|GONE|FAILED). pywebpush로 보낸다."""
from __future__ import annotations

import base64
import logging
from typing import Mapping

import ipaddress
import socket
from urllib.parse import urlsplit

log = logging.getLogger("survey_service.alerts")


# ---------------------------------------------------------------- 주소 검사(SSRF 방어, 감사 B6): staff가 등록한 URL로 서버가 요청을 보내므로 공개 인터넷 주소만 허용한다.
# 등록 때는 문법 검사(endpoint_syntax_error, DNS 없음), 보낼 때는 해석한 주소가 모두 공개인지(host_is_public). 한계: DNS 재바인딩은 막지 못한다. 리다이렉트는 따르지 않는다.

_INTERNAL_SUFFIXES = (".local", ".localdomain", ".internal", ".lan", ".home", ".corp", ".intranet")


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")


def _ip_public(ip) -> bool:
    """공개(전역) 주소인가. IPv6에 IPv4를 품은 형태(IPv4-mapped, NAT64, 6to4, IPv4-compatible)는 품은 IPv4까지 공개여야 하고, Teredo와 NAT64 로컬 접두는 막는다."""
    if getattr(ip, "ipv4_mapped", None) is not None:
        return _ip_public(ip.ipv4_mapped)
    if ip.version == 6:
        if ip in _TEREDO or ip in _NAT64_LOCAL:
            return False
        if ip in _NAT64:
            return _ip_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip in _SIX_TO_FOUR:
            return _ip_public(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF))
        if int(ip) >> 32 == 0:                       # ::/96 IPv4-compatible(::a.b.c.d)
            return False
    return ip.is_global and not ip.is_multicast


def endpoint_syntax_error(url: str) -> str | None:
    """등록 때 쓰는 문법 검사(DNS를 부르지 않는다). 문제가 있으면 사유 문장, 없으면 None."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return "endpoint URL 형식이 잘못됐다"
    if u.scheme != "https" or not u.hostname:
        return "endpoint는 https URL이어야 한다"
    if u.username or u.password:
        return "endpoint에 사용자 정보를 넣을 수 없다"
    if port not in (None, 443):
        return "endpoint 포트는 443이어야 한다"
    host = u.hostname.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
            return "endpoint는 공개 호스트 이름이어야 한다"
        return None
    return None if _ip_public(ip) else "endpoint는 공개 주소여야 한다"


_getaddrinfo = socket.getaddrinfo      # 이름 해석 함수(시험이 가짜 DNS로 바꾼다: 실제 DNS에 기대지 않는다)


def endpoint_error(url: str) -> str | None:
    """등록 때 쓰는 전체 검사(R3.18): 문법 + 호스트 이름이 RFC 6890 특수 목적 주소가 아닌 공인 주소로만 해석될 것(발송 때와 같은 검사). 해석하지 못하면 쓸 수 없다. 문제가 있으면 사유, 없으면 None."""
    why = endpoint_syntax_error(url)
    if why:
        return why
    if not host_is_public(url):
        return "endpoint의 호스트가 공인 주소로 해석되지 않는다"
    return None


def host_is_public(url: str) -> bool:
    """보내기 직전 확인: 호스트를 해석해 모든 주소가 공개 주소인가. 해석 실패는 False."""
    try:
        host = urlsplit(url).hostname
        if not host:
            return False
        infos = _getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    except (OSError, ValueError):
        return False
    return bool(addrs) and all(_ip_public(a) for a in addrs)


def _system_ca_bundle() -> str:
    """시스템 CA 묶음 경로(OS 신뢰 저장소 파일). 없으면 certifi의 묶음."""
    import ssl
    import os
    cafile = ssl.get_default_verify_paths().openssl_cafile      # 컴파일된 기본 경로: SSL_CERT_FILE 환경 변수(내부 루트)를 따르는 `cafile`이 아니다
    if cafile and os.path.exists(cafile):
        return cafile
    import certifi
    return certifi.where()


MAX_ATTEMPTS = 3          # R3.18: 한 알림당 최대 3번 시도, 처음부터 60초 안(시작값·측정 안 함). RFC 8030의 재시도 권고를 횟수·시간으로 묶은 값
TOTAL_SECONDS = 60.0
BACKOFF_SECONDS = (1.0, 2.0, 4.0)      # Retry-After가 없을 때의 지수 백오프


def _retry_after_seconds(value) -> float | None:
    """Retry-After(RFC 9110 10.2.3): 초 정수 또는 HTTP 날짜. 읽을 수 없으면 None."""
    if value is None:
        return None
    v = str(value).strip()
    if v.isdigit():
        return float(v)
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        return max(0.0, (parsedate_to_datetime(v) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class WebPushSender:
    """pywebpush로 보낸다. VAPID 비밀키는 환경 변수에서 받은 값을 생성자에 넘긴다(여기서 환경을 읽지 않는다).
    바깥으로 나가는 유일한 호출이다(R6.4): egress_proxy(OPENIT_EGRESS_PROXY, 자기 영역 게이트웨이)가 있으면 그 CONNECT 터널로만 나간다(TLS는 푸시 서비스까지 끝까지 유지, 시스템 루트로 검증)."""

    def __init__(self, vapid_private_key: str, subject: str, timeout: float = 3.0, ttl: int = 3600, allow_private_hosts: bool = False,
                 egress_proxy: str | None = None, sleep=None, monotonic=None) -> None:
        import time as _time
        for noisy in ("urllib3", "requests", "pywebpush"):      # 이 라이브러리들은 DEBUG에서 요청 경로(= 구독 endpoint)와 호스트를 로그에 남긴다: 로그에 endpoint를 남기지 않는다(R3.18, R6.3)
            logging.getLogger(noisy).setLevel(logging.WARNING)
        self._sleep, self._mono = sleep or _time.sleep, monotonic or _time.monotonic      # 시계 주입: 재시도 시험이 실제 시간을 재지 않게
        if egress_proxy is not None:
            from boundary_contracts.clients import require_egress_proxy
            require_egress_proxy(egress_proxy)
        self._egress = egress_proxy
        from py_vapid import Vapid
        try:                                                      # 키 형식을 시작할 때 확인한다(값은 어디에도 남기지 않는다)
            vapid = (Vapid.from_pem(vapid_private_key.encode()) if "BEGIN" in vapid_private_key
                     else Vapid.from_string(vapid_private_key))   # PEM 또는 base64url DER(웹 푸시 표준 표기)
        except Exception:
            raise ValueError("VAPID 비밀키 형식이 잘못됐다") from None
        if not subject.startswith(("mailto:", "https://")):
            raise ValueError("VAPID subject는 mailto: 또는 https:// 이어야 한다")
        self._vapid, self._subject, self._timeout, self._ttl = vapid, subject, timeout, ttl
        self._allow_private = allow_private_hosts      # 개발·시험 전용. 운영 기본은 공개 주소만(netguard)
        from cryptography.hazmat.primitives import serialization
        raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()      # applicationServerKey(공개키, 비밀 아님)

    def _attempt(self, subscription: Mapping, payload: bytes, topic: str | None) -> tuple[str, float | None]:
        """한 번 보낸다 -> (결과, Retry-After 초). 결과: ACCEPTED, GONE(404·410), RETRY(429·5xx·불통·시간 초과·도중 끊김), FAILED(다시 보내지 않는다: 401·403·그 밖 4xx·TLS 검증·이름 해석)."""
        from pywebpush import WebPushException, webpush
        import requests
        if not self._allow_private and not host_is_public(str(subscription.get("endpoint", ""))):
            log.warning("웹 푸시 대상이 공개 주소가 아니거나 이름을 해석하지 못해 보내지 않는다")      # SSRF 방어(URL은 남기지 않는다). 같은 요청은 같은 결과라 다시 보내지 않는다
            return "FAILED", None
        try:
            session = requests.Session()
            session.verify = _system_ca_bundle()      # R3.18: 공개 푸시 서비스는 시스템(공개) 루트로 검증한다. 내부 루트(SSL_CERT_FILE·REQUESTS_CA_BUNDLE)와 섞지 않는다: 세션에 경로를 명시하면 requests는 환경 변수를 쓰지 않는다
            session.trust_env = False                  # REQUESTS_CA_BUNDLE·CURL_CA_BUNDLE·HTTPS_PROXY 환경 변수를 읽지 않는다: 나가는 길은 egress_proxy 하나다
            session.proxies = {"https": self._egress, "http": self._egress} if self._egress else {}
            session.max_redirects = 0                              # 리다이렉트를 따르지 않는다(다른 내부 주소로 돌려지는 것을 막는다)
            headers = {"Urgency": "high"}
            if topic:
                headers["Topic"] = topic
            webpush(subscription_info=dict(subscription), data=payload.decode("utf-8"), vapid_private_key=self._vapid,
                    vapid_claims={"sub": self._subject}, timeout=self._timeout, ttl=self._ttl, headers=headers, requests_session=session)
            return "ACCEPTED", None
        except WebPushException as e:
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if status in (404, 410):
                return "GONE", None
            log.warning("웹 푸시 거부 status=%s", status)
            if status == 429 or (isinstance(status, int) and status >= 500):
                return "RETRY", _retry_after_seconds(getattr(resp, "headers", {}).get("Retry-After"))
            if status is None:                       # 응답 없이 난 pywebpush 오류(암호화 입력 오류 등)는 같은 요청이 같은 결과다
                return "FAILED", None
            return "FAILED", None                    # 401·403(VAPID 거절)·그 밖의 4xx
        except (requests.exceptions.SSLError, requests.exceptions.ProxyError, requests.exceptions.InvalidURL) as e:
            log.warning("웹 푸시 실패(다시 보내지 않음) error=%s", type(e).__name__)      # TLS 검증 실패·게이트웨이 거절·잘못된 주소: 같은 요청은 같은 결과
            return "FAILED", None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            log.warning("웹 푸시 일시 장애 error=%s", type(e).__name__)      # 불통·연결 시간 초과·시간 한도 초과·응답 도중 끊김
            return "RETRY", None
        except Exception as e:                                   # 잘못된 구독 키 등. 내용은 남기지 않는다
            log.warning("웹 푸시 실패 error=%s", type(e).__name__)
            return "FAILED", None

    def send(self, subscription: Mapping, payload: bytes, topic: str | None = None, on_first=None) -> str:
        """결과 ACCEPTED|GONE|FAILED. 일시 장애(RETRY)는 최대 MAX_ATTEMPTS번·처음부터 TOTAL_SECONDS 안에서 Retry-After(없으면 1·2·4초)를 따라 다시 보낸다.
        on_first(결과)는 첫 시도가 끝나면 한 번 불린다(RETRY면 'RETRY'): 호출자가 첫 결과로 전달 여부를 먼저 판단하고 재시도는 뒤에서 계속되게 한다."""
        start = self._mono()
        out, retry_after = "FAILED", None
        for n in range(MAX_ATTEMPTS):
            out, retry_after = self._attempt(subscription, payload, topic)
            if n == 0 and on_first is not None:
                on_first(out)
            if out != "RETRY":
                return out
            if n == MAX_ATTEMPTS - 1:
                break
            wait = retry_after if retry_after is not None else BACKOFF_SECONDS[n]
            if self._mono() - start + wait >= TOTAL_SECONDS:      # 기다린 뒤에는 60초 한도를 넘는다: 더 보내지 않는다
                break
            self._sleep(wait)
        return "FAILED"
