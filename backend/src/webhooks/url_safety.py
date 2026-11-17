"""Validate webhook target URLs and resolve them to a checked address."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048


class WebhookUrlError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedTarget:
    url: str
    host: str
    ip: str


def validate_webhook_url(url: str, *, allow_private: bool) -> str:
    """Syntax, scheme and length only; returns the stripped URL."""
    cleaned = (url or "").strip()
    if not cleaned or len(cleaned) > MAX_URL_LENGTH:
        raise WebhookUrlError(f"URL must be 1-{MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(cleaned)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as exc:
        raise WebhookUrlError("URL is not valid") from exc
    allowed = ("http", "https") if allow_private else ("https",)
    if parts.scheme not in allowed:
        raise WebhookUrlError("URL must use " + " or ".join(allowed))
    if not hostname:
        raise WebhookUrlError("URL needs a host")
    return cleaned


def _check_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool) -> None:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address.is_unspecified or address.is_multicast:
        raise WebhookUrlError("URL points at a blocked address")
    if allow_private:
        return
    if address.is_loopback or address.is_private or address.is_link_local or address.is_reserved:
        raise WebhookUrlError("URL points at a private address")


async def resolve_target(url: str, *, allow_private: bool) -> ResolvedTarget:
    cleaned = validate_webhook_url(url, allow_private=allow_private)
    parts = urlsplit(cleaned)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebhookUrlError("Host could not be resolved") from exc
    if not infos:
        raise WebhookUrlError("Host could not be resolved")
    addresses = []
    for info in infos:
        raw = info[4][0].split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise WebhookUrlError("Host resolved to an invalid address") from exc
        _check_address(address, allow_private=allow_private)
        addresses.append(raw)
    return ResolvedTarget(url=cleaned, host=host, ip=addresses[0])
