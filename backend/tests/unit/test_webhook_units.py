import hashlib
import hmac
import socket
from datetime import datetime, timezone

import pytest

from src.webhooks import payload as payload_mod
from src.webhooks import url_safety
from src.webhooks.signing import delivery_headers, generate_secret, sign
from src.webhooks.url_safety import WebhookUrlError, resolve_target, validate_webhook_url


def test_sign_matches_reference():
    expected = "v1=" + hmac.new(b"whsec_x", b'1700000000.{"a":1}', hashlib.sha256).hexdigest()
    assert sign("whsec_x", 1700000000, b'{"a":1}') == expected


def test_headers_complete():
    headers = delivery_headers(secret="whsec_x", event="task.completed", delivery_id="d1", timestamp=1700000000, body=b"{}")
    assert set(headers) == {
        "Content-Type",
        "User-Agent",
        "X-SupoClip-Event",
        "X-SupoClip-Delivery",
        "X-SupoClip-Timestamp",
        "X-SupoClip-Signature",
    }
    assert headers["X-SupoClip-Timestamp"] == "1700000000"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == "SupoClip-Webhooks/1"
    assert headers["X-SupoClip-Signature"] == sign("whsec_x", 1700000000, b"{}")


def test_generate_secret_prefix_and_entropy():
    a, b = generate_secret(), generate_secret()
    assert a.startswith("whsec_") and len(a) >= 46
    assert a != b


def test_validate_rejects_bad_syntax():
    for bad in ["ftp://x", "https://", "https://" + "a" * 2050, "not a url"]:
        with pytest.raises(WebhookUrlError):
            validate_webhook_url(bad, allow_private=True)
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("http://example.com", allow_private=False)
    assert validate_webhook_url("  http://example.com/x  ", allow_private=True) == "http://example.com/x"
    assert validate_webhook_url("https://example.com", allow_private=False) == "https://example.com"


def _fake_getaddrinfo(*addresses):
    async def fake(self, host, port, **kwargs):
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 443))
            for a in addresses
        ]

    return fake


def _patch(monkeypatch, *addresses):
    import asyncio

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", _fake_getaddrinfo(*addresses))


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "169.254.1.1", "::1", "fd00::1", "0.0.0.0"])
async def test_resolve_blocks_private_when_disallowed(monkeypatch, ip):
    _patch(monkeypatch, ip)
    with pytest.raises(WebhookUrlError):
        await resolve_target("https://hooks.example.com/x", allow_private=False)


async def test_resolve_allows_private_when_allowed(monkeypatch):
    _patch(monkeypatch, "10.1.2.3")
    target = await resolve_target("http://hooks.local:8080/x", allow_private=True)
    assert target.url == "http://hooks.local:8080/x"
    assert target.host == "hooks.local"
    assert target.ip == "10.1.2.3"
    for ip in ["0.0.0.0", "224.0.0.1"]:
        _patch(monkeypatch, ip)
        with pytest.raises(WebhookUrlError):
            await resolve_target("http://hooks.local/x", allow_private=True)


async def test_resolve_checks_every_address(monkeypatch):
    _patch(monkeypatch, "93.184.216.34", "10.0.0.1")
    with pytest.raises(WebhookUrlError):
        await resolve_target("https://hooks.example.com/x", allow_private=False)


def test_payload_shape():
    created = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
    task = {
        "id": "T",
        "status": "completed",
        "workflow": "highlights",
        "completed_at": datetime(2026, 11, 17, 9, 59, 0, tzinfo=timezone.utc),
        "source_title": "Title",
        "source_url": "https://youtube.com/x",
        "source_type": "youtube",
    }
    clips = [
        {"id": "C1", "clip_order": 1, "hook_title": None, "text": "t1", "start_time": "01:23", "end_time": "02:05",
         "duration": 42.0, "virality_score": 87, "workflow_role": None, "part_number": None},
        {"id": "C2", "clip_order": 2, "hook_title": "Hook", "text": "t2", "start_time": "03:00", "end_time": "03:30",
         "duration": 30.0, "virality_score": 70, "workflow_role": "part", "part_number": 2},
    ]
    p = payload_mod.build_task_completed_payload(
        delivery_id="D", created_at=created, task=task, clips=clips,
        public_api_url="https://api.x/", app_base_url="https://app.x/",
    )
    assert p["event"] == "task.completed"
    assert p["delivery_id"] == "D"
    assert p["created_at"] == "2026-11-17T10:00:00Z"
    assert p["task"]["url"] == "https://app.x/tasks/T"
    assert p["task"]["clips_count"] == 2
    assert p["task"]["completed_at"] == "2026-11-17T09:59:00Z"
    assert p["task"]["source_type"] == "youtube"
    assert [c["id"] for c in p["clips"]] == ["C1", "C2"]
    assert [c["order"] for c in p["clips"]] == [1, 2]
    assert p["clips"][0]["download_url"] == "https://api.x/tasks/T/clips/C1/file?download=true"
    assert p["clips"][0]["hook_title"] is None and p["clips"][0]["workflow_role"] is None
    assert p["clips"][1]["part_number"] == 2


def test_test_payload():
    created = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
    assert payload_mod.build_test_payload(delivery_id="D", created_at=created) == {
        "event": "webhook.test",
        "delivery_id": "D",
        "created_at": "2026-11-17T10:00:00Z",
        "message": "SupoClip webhook test",
    }
