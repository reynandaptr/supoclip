import pytest
from sqlalchemy import text

from tests.fixtures.factories import create_user

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean_webhook_tables(db_session):
    await db_session.execute(text("DELETE FROM webhook_deliveries"))
    await db_session.execute(text("DELETE FROM user_webhooks"))
    await db_session.commit()


async def test_full_lifecycle(client, db_session, auth_headers):
    await create_user(db_session, user_id="user-1")

    got = await client.get("/webhooks/", headers=auth_headers)
    assert got.status_code == 200
    assert got.json() == {"configured": False}

    # Testing before configuring is a conflict.
    assert (await client.post("/webhooks/test", headers=auth_headers)).status_code == 409

    saved = await client.put(
        "/webhooks/", headers=auth_headers, json={"url": "https://example.com/hook"}
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["secret"].startswith("whsec_")
    assert body["enabled"] is True

    got = await client.get("/webhooks/", headers=auth_headers)
    data = got.json()
    assert data["configured"] is True
    assert data["url"] == "https://example.com/hook"
    assert data["secret_hint"]
    assert "secret" not in data

    rotated = await client.post("/webhooks/rotate-secret", headers=auth_headers)
    assert rotated.status_code == 200
    assert rotated.json()["secret"].startswith("whsec_")
    assert rotated.json()["secret"] != body["secret"]

    tested = await client.post("/webhooks/test", headers=auth_headers)
    assert tested.status_code == 200
    assert tested.json()["delivery_id"]

    deleted = await client.delete("/webhooks/", headers=auth_headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}

    assert (await client.get("/webhooks/", headers=auth_headers)).json() == {
        "configured": False
    }
    assert (
        await client.post("/webhooks/rotate-secret", headers=auth_headers)
    ).status_code == 404


async def test_invalid_url_400(client, db_session, auth_headers):
    await create_user(db_session, user_id="user-1")
    resp = await client.put(
        "/webhooks/", headers=auth_headers, json={"url": "ftp://example.com/hook"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]


async def test_api_key_auth_rejected(client):
    resp = await client.get(
        "/webhooks/", headers={"Authorization": "Bearer sk_anything"}
    )
    assert resp.status_code == 401
