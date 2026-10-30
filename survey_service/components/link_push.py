"""직원 기기 웹 푸시 전송(보내는 쪽). 입력 PushDelivery(구독 + StaffPushPayload 본문) -> 출력 PushOutcome(ACCEPTED|GONE|FAILED). pywebpush로 보낸다."""
from __future__ import annotations

import base64
import logging
from typing import Mapping

from ..wiring import netguard

log = logging.getLogger("survey_service.alerts")


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
        if not self._allow_private and not netguard.host_is_public(str(subscription.get("endpoint", ""))):
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


