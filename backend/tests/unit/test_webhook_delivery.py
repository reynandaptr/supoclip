import asyncio
import json
import socket

import httpx
import pytest

from src.webhooks import delivery
from src.webhooks.delivery import BACKOFF_SECONDS, next_retry_delay, send_delivery
from src.webhooks.signing import sign

SECRET = "whsec_test"
PUBLIC_IP = "93.184.216.34"


def _patch_resolution(monkeypatch, *addresses):
    async def fake(self, host, port, **kwargs):
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 443))
            for a in addresses
        ]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake)


def _row(url="https://hooks.example.com/receive?x=1"):
    return {
        "id": "delivery-1",
        "user_id": "user-1",
        "event": "task.completed",
        "url": url,
        "payload": {"event": "task.completed", "task": {"id": "t1", "title": "héllo"}},
        "attempts": 0,
    }


class _Recorder:
    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.respond(request)


def _client(recorder):
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder), timeout=10, follow_redirects=False)


def test_backoff_schedule():
    assert BACKOFF_SECONDS == (60, 300, 900, 3600, 10800, 21600, 43200)
    assert [next_retry_delay(n) for n in range(1, 8)] == [60, 300, 900, 3600, 10800, 21600, 43200]
    assert next_retry_delay(8) is None
    assert next_retry_delay(9) is None


async def test_2xx_is_ok(monkeypatch):
    _patch_resolution(monkeypatch, PUBLIC_IP)
    recorder = _Recorder(lambda request: httpx.Response(204))
    row = _row()
    async with _client(recorder) as client:
        result = await send_delivery(row, secret=SECRET, client=client, allow_private=False)

    assert result.ok is True
    assert result.status_code == 204
    assert result.error is None
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.method == "POST"
    # Connected to the checked address, but addressed to the original host.
    assert request.url.host == PUBLIC_IP
    assert request.url.path == "/receive"
    assert request.url.query == b"x=1"
    assert request.headers["host"] == "hooks.example.com"
    assert request.extensions.get("sni_hostname") == "hooks.example.com"
    body = request.content
    assert body == json.dumps(row["payload"], separators=(",", ":"), default=str).encode()
    timestamp = int(request.headers["x-supoclip-timestamp"])
    assert request.headers["x-supoclip-signature"] == sign(SECRET, timestamp, body)
    assert request.headers["x-supoclip-event"] == "task.completed"
    assert request.headers["x-supoclip-delivery"] == "delivery-1"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"] == "SupoClip-Webhooks/1"


async def test_host_header_keeps_an_explicit_port(monkeypatch):
    _patch_resolution(monkeypatch, PUBLIC_IP)
    recorder = _Recorder(lambda request: httpx.Response(200))
    async with _client(recorder) as client:
        result = await send_delivery(
            _row("http://hooks.example.com:8080/x"), secret=SECRET, client=client, allow_private=True
        )

    assert result.ok is True
    request = recorder.requests[0]
    assert str(request.url) == f"http://{PUBLIC_IP}:8080/x"
    assert request.headers["host"] == "hooks.example.com:8080"
    assert "sni_hostname" not in request.extensions


async def test_ipv6_address_is_bracketed(monkeypatch):
    _patch_resolution(monkeypatch, "2606:2800:220:1:248:1893:25c8:1946")
    recorder = _Recorder(lambda request: httpx.Response(200))
    async with _client(recorder) as client:
        await send_delivery(_row(), secret=SECRET, client=client, allow_private=False)

    request = recorder.requests[0]
    assert str(request.url).startswith("https://[2606:2800:220:1:248:1893:25c8:1946]/receive")
    assert request.headers["host"] == "hooks.example.com"


async def test_500_is_failure(monkeypatch):
    _patch_resolution(monkeypatch, PUBLIC_IP)
    recorder = _Recorder(lambda request: httpx.Response(500, text="boom"))
    async with _client(recorder) as client:
        result = await send_delivery(_row(), secret=SECRET, client=client, allow_private=False)

    assert result.ok is False
    assert result.status_code == 500
    assert "500" in result.error


async def test_redirect_is_not_followed(monkeypatch):
    _patch_resolution(monkeypatch, PUBLIC_IP)
    recorder = _Recorder(
        lambda request: httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
    )
    async with _client(recorder) as client:
        result = await send_delivery(_row(), secret=SECRET, client=client, allow_private=False)

    assert result.ok is False
    assert result.status_code == 302
    assert len(recorder.requests) == 1


async def test_timeout_reschedules(monkeypatch):
    _patch_resolution(monkeypatch, PUBLIC_IP)

    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    async with _client(_Recorder(timeout)) as client:
        result = await send_delivery(_row(), secret=SECRET, client=client, allow_private=False)

    assert result.ok is False
    assert result.status_code is None
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


async def test_blocked_resolution_is_never_sent(monkeypatch):
    _patch_resolution(monkeypatch, "10.0.0.1")
    recorder = _Recorder(lambda request: httpx.Response(200))
    async with _client(recorder) as client:
        result = await send_delivery(_row(), secret=SECRET, client=client, allow_private=False)

    assert result.ok is False
    assert result.status_code is None
    assert result.error
    assert recorder.requests == []


def test_module_exports_the_cron_entry_point():
    assert asyncio.iscoroutinefunction(delivery.deliver_webhooks)
