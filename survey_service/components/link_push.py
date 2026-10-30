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


def host_is_public(url: str) -> bool:
    """보내기 직전 확인: 호스트를 해석해 모든 주소가 공개 주소인가. 해석 실패는 False."""
    try:
        host = urlsplit(url).hostname
        if not host:
            return False
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    except (OSError, ValueError):
        return False
    return bool(addrs) and all(_ip_public(a) for a in addrs)


class WebPushSender:
    """pywebpush로 보낸다. VAPID 비밀키는 환경 변수에서 받은 값을 생성자에 넘긴다(여기서 환경을 읽지 않는다)."""

    def __init__(self, vapid_private_key: str, subject: str, timeout: float = 3.0, ttl: int = 3600, allow_private_hosts: bool = False) -> None:
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

    def send(self, subscription: Mapping, payload: bytes) -> str:
        from pywebpush import WebPushException, webpush
        if not self._allow_private and not host_is_public(str(subscription.get("endpoint", ""))):
            log.warning("웹 푸시 대상이 공개 주소가 아니어서 보내지 않는다")          # SSRF 방어(URL은 남기지 않는다)
            return "FAILED"
        try:
            import requests
            session = requests.Session()
            session.max_redirects = 0                              # 리다이렉트를 따르지 않는다(다른 내부 주소로 돌려지는 것을 막는다)
            webpush(subscription_info=dict(subscription), data=payload.decode("utf-8"), vapid_private_key=self._vapid,
                    vapid_claims={"sub": self._subject}, timeout=self._timeout, ttl=self._ttl,
                    headers={"Urgency": "high"}, requests_session=session)
            return "ACCEPTED"
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                return "GONE"
            log.warning("웹 푸시 거부 status=%s", status)
            return "FAILED"
        except Exception as e:                                   # 네트워크 오류, 잘못된 구독 키 등. 내용은 남기지 않는다
            log.warning("웹 푸시 실패 error=%s", type(e).__name__)
            return "FAILED"


