import pytest
from sqlalchemy import text

from tests.fixtures.factories import (
    create_clip,
    create_source,
    create_task,
    create_user,
)

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


async def _setup_task(db_session, client, auth_headers, *, status="completed", clips=0):
    await create_user(db_session, user_id="user-1")
    source = await create_source(db_session)
    task = await create_task(
        db_session, user_id="user-1", source_id=source["id"], status=status
    )
    for order in range(clips):
        clip = await create_clip(db_session, task_id=task["id"])
        await db_session.execute(
            text("UPDATE generated_clips SET clip_order = :o WHERE id = :id"),
            {"o": order + 10, "id": clip["id"]},
        )
        await db_session.commit()
    return task["id"]


async def _enable(client, auth_headers):
    resp = await client.put(
        "/webhooks/", headers=auth_headers, json={"url": "https://example.com/hook"}
    )
    assert resp.status_code == 200


async def _rows(db_session):
    result = await db_session.execute(
        text(
            "SELECT id, task_id, event, status, task_completed_at, payload "
            "FROM webhook_deliveries ORDER BY created_at"
        )
    )
    return [dict(r) for r in result.mappings().all()]


async def test_send_task_creates_new_delivery_each_call(client, db_session, auth_headers):
    task_id = await _setup_task(db_session, client, auth_headers, clips=2)
    await _enable(client, auth_headers)

    first = await client.post(f"/webhooks/tasks/{task_id}/send", headers=auth_headers)
    second = await client.post(f"/webhooks/tasks/{task_id}/send", headers=auth_headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["delivery_id"] != second.json()["delivery_id"]

    rows = await _rows(db_session)
    assert len(rows) == 2
    for row in rows:
        assert row["event"] == "task.completed"
        assert row["status"] == "pending"
        assert row["task_id"] == task_id
        assert row["task_completed_at"] is None
        assert row["payload"]["task"]["id"] == task_id
        assert len(row["payload"]["clips"]) == 2
        assert row["payload"]["delivery_id"] == str(row["id"])


async def test_send_task_ignores_enabled_since(client, db_session, auth_headers):
    task_id = await _setup_task(db_session, client, auth_headers)
    await db_session.execute(
        text("UPDATE tasks SET completed_at = now() - interval '2 days' WHERE id = :id"),
        {"id": task_id},
    )
    await db_session.commit()
    await _enable(client, auth_headers)

    resp = await client.post(f"/webhooks/tasks/{task_id}/send", headers=auth_headers)
    assert resp.status_code == 200
    assert len(await _rows(db_session)) == 1


async def test_send_task_other_users_task_404(client, db_session, auth_headers):
    await create_user(db_session, user_id="other")
    source = await create_source(db_session)
    task = await create_task(db_session, user_id="other", source_id=source["id"])
    await create_user(db_session, user_id="user-1")
    await _enable(client, auth_headers)

    resp = await client.post(f"/webhooks/tasks/{task['id']}/send", headers=auth_headers)
    assert resp.status_code == 404
    missing = await client.post("/webhooks/tasks/nope/send", headers=auth_headers)
    assert missing.status_code == 404
    assert await _rows(db_session) == []


async def test_send_task_not_completed_409(client, db_session, auth_headers):
    task_id = await _setup_task(db_session, client, auth_headers, status="error")
    await _enable(client, auth_headers)

    resp = await client.post(f"/webhooks/tasks/{task_id}/send", headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Only completed tasks can be sent"


async def test_send_task_without_webhook_or_disabled_409(client, db_session, auth_headers):
    task_id = await _setup_task(db_session, client, auth_headers)

    resp = await client.post(f"/webhooks/tasks/{task_id}/send", headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Set up and enable the webhook first"

    await client.put(
        "/webhooks/",
        headers=auth_headers,
        json={"url": "https://example.com/hook", "enabled": False},
    )
    resp = await client.post(f"/webhooks/tasks/{task_id}/send", headers=auth_headers)
    assert resp.status_code == 409
    assert await _rows(db_session) == []


async def test_send_task_api_key_rejected(client):
    resp = await client.post(
        "/webhooks/tasks/any/send", headers={"Authorization": "Bearer sk_anything"}
    )
    assert resp.status_code == 401
