"""웹 푸시 endpoint의 SSRF 방어(감사 B6). staff가 등록한 URL로 서버가 요청을 보내므로 공개 인터넷 주소만 허용한다.

- 등록 때(문법 검사, DNS 없음): https, 사용자 정보 없음, 포트 443(또는 생략), 호스트가 localhost·단일 라벨·내부용 이름이 아니고 IP 리터럴이면 공개 주소.
- 보낼 때(`host_is_public`): 호스트를 해석해 나온 주소가 **모두** 전역(공개) 주소일 때만 보낸다. 사설(10/8, 172.16/12, 192.168/16), 루프백, 링크 로컬(169.254/16: 클라우드 메타데이터 포함),
  CGNAT(100.64/10), 멀티캐스트, 예약 주소, IPv6의 같은 범위(IPv4 매핑 포함)는 막는다.
- 한계: 해석과 연결 사이에 DNS 응답이 바뀌는 공격(DNS 재바인딩)은 막지 못한다(연결을 해석한 주소에 고정하지 않는다). 리다이렉트는 따르지 않는다(WebPushSender).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

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
