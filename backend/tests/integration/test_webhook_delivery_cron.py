import asyncio
import socket
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from src.repositories.webhook_repository import WebhookRepository
from src.runtime_settings import encrypt_setting_value
from src.webhooks import delivery
from src.webhooks.signing import sign
from tests.fixtures.factories import create_user

pytestmark = pytest.mark.asyncio

URL = "https://hooks.example.com/receive"
SECRET = "whsec_integration"


@pytest.fixture(autouse=True)
async def _clean_webhook_tables(db_session):
    await db_session.execute(text("DELETE FROM webhook_deliveries"))
    await db_session.execute(text("DELETE FROM user_webhooks"))
    await db_session.commit()
    yield
    await db_session.rollback()
    await db_session.execute(text("DELETE FROM webhook_deliveries"))
    await db_session.execute(text("DELETE FROM user_webhooks"))
    await db_session.commit()


@pytest.fixture(autouse=True)
def _public_resolution(monkeypatch):
    async def fake(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake)


def _mock_client(monkeypatch, statuses):
    requests = []
    replies = iter(statuses)

    def handler(request):
        requests.append(request)
        return httpx.Response(next(replies))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        delivery.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return requests


async def test_deliver_webhooks_end_to_end(db_session, monkeypatch):
    user_id = (await create_user(db_session))["id"]
    await WebhookRepository.upsert_config(
        db_session, user_id, url=URL, enabled=True, secret_enc=encrypt_setting_value(SECRET)
    )
    delivery_id = str(uuid4())
    await WebhookRepository.insert_delivery(
        db_session,
        delivery_id=delivery_id,
        user_id=user_id,
        task_id=None,
        event="webhook.test",
        url=URL,
        payload={"event": "webhook.test", "delivery_id": delivery_id},
        task_completed_at=None,
    )
    await db_session.commit()
    requests = _mock_client(monkeypatch, [500, 204])

    first = await delivery.deliver_webhooks({})
    assert first["sent"] == 1
    assert first["failed"] == 1
    assert first["delivered"] == 0
    row = (
        await db_session.execute(
            text(
                "SELECT status, attempts, last_status_code, next_attempt_at > now() AS later "
                "FROM webhook_deliveries WHERE id = :id"
            ),
            {"id": delivery_id},
        )
    ).mappings().one()
    assert dict(row) == {"status": "pending", "attempts": 1, "last_status_code": 500, "later": True}

    # Not due yet: a second run sends nothing.
    assert (await delivery.deliver_webhooks({}))["sent"] == 0

    await db_session.execute(
        text("UPDATE webhook_deliveries SET next_attempt_at = now() WHERE id = :id"), {"id": delivery_id}
    )
    await db_session.commit()

    second = await delivery.deliver_webhooks({})
    assert second["sent"] == 1
    assert second["delivered"] == 1
    row = (
        await db_session.execute(
            text("SELECT status, attempts, last_status_code FROM webhook_deliveries WHERE id = :id"),
            {"id": delivery_id},
        )
    ).mappings().one()
    assert dict(row) == {"status": "delivered", "attempts": 2, "last_status_code": 204}

    assert len(requests) == 2
    last = requests[-1]
    assert last.headers["host"] == "hooks.example.com"
    assert last.headers["x-supoclip-delivery"] == delivery_id
    timestamp = int(last.headers["x-supoclip-timestamp"])
    assert last.headers["x-supoclip-signature"] == sign(SECRET, timestamp, last.content)


async def test_disabled_webhook_cancels_instead_of_sending(db_session, monkeypatch):
    user_id = (await create_user(db_session))["id"]
    await WebhookRepository.upsert_config(
        db_session, user_id, url=URL, enabled=True, secret_enc=encrypt_setting_value(SECRET)
    )
    delivery_id = str(uuid4())
    await WebhookRepository.insert_delivery(
        db_session,
        delivery_id=delivery_id,
        user_id=user_id,
        task_id=None,
        event="webhook.test",
        url=URL,
        payload={"event": "webhook.test"},
        task_completed_at=None,
    )
    # Switched off directly, so the pending row survives until the cron sees it.
    await db_session.execute(
        text("UPDATE user_webhooks SET enabled = false WHERE user_id = :u"), {"u": user_id}
    )
    await db_session.commit()
    requests = _mock_client(monkeypatch, [204])

    result = await delivery.deliver_webhooks({})

    assert result["sent"] == 0
    assert requests == []
    status = (
        await db_session.execute(
            text("SELECT status FROM webhook_deliveries WHERE id = :id"), {"id": delivery_id}
        )
    ).scalar_one()
    assert status == "cancelled"


async def test_unreadable_secret_counts_as_a_failed_attempt(db_session, monkeypatch):
    user_id = (await create_user(db_session))["id"]
    await WebhookRepository.upsert_config(
        db_session, user_id, url=URL, enabled=True, secret_enc="not-a-valid-ciphertext"
    )
    delivery_id = str(uuid4())
    await WebhookRepository.insert_delivery(
        db_session,
        delivery_id=delivery_id,
        user_id=user_id,
        task_id=None,
        event="webhook.test",
        url=URL,
        payload={"event": "webhook.test"},
        task_completed_at=None,
    )
    await db_session.commit()
    requests = _mock_client(monkeypatch, [204])

    result = await delivery.deliver_webhooks({})

    assert result["sent"] == 0
    assert result["failed"] == 1
    assert requests == []
    row = (
        await db_session.execute(
            text("SELECT status, attempts, last_error FROM webhook_deliveries WHERE id = :id"),
            {"id": delivery_id},
        )
    ).mappings().one()
    assert dict(row) == {"status": "pending", "attempts": 1, "last_error": "webhook secret unreadable"}
