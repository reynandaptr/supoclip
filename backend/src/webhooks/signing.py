"""Secret generation and request signing for outgoing webhooks."""

from __future__ import annotations

import hashlib
import hmac
import secrets

SECRET_PREFIX = "whsec_"
USER_AGENT = "SupoClip-Webhooks/1"


def generate_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode() + b"." + body
    return "v1=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def delivery_headers(*, secret: str, event: str, delivery_id: str, timestamp: int, body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-SupoClip-Event": event,
        "X-SupoClip-Delivery": delivery_id,
        "X-SupoClip-Timestamp": str(timestamp),
        "X-SupoClip-Signature": sign(secret, timestamp, body),
    }
