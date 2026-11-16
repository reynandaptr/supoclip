from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from src.runtime_settings import decrypt_setting_value
from src.webhooks.service import WebhookService
from src.webhooks.url_safety import WebhookUrlError
from tests.fixtures.factories import create_clip, create_source, create_task, create_user

pytestmark = pytest.mark.asyncio

URL = "https://example.com/hook"


@pytest.fixture(autouse=True)
async def _clean_webhook_tables(db_session):
    await db_session.execute(text("DELETE FROM webhook_deliveries"))
    await db_session.execute(text("DELETE FROM user_webhooks"))
    await db_session.commit()


async def _user(db):
    return (await create_user(db))["id"]


async def _completed_task(db, user_id, clips=0):
    source = await create_source(db)
    task = await create_task(db, user_id=user_id, source_id=source["id"])
    await db.execute(
        text("UPDATE tasks SET completed_at = :c WHERE id = :id"),
        {"c": datetime.now(timezone.utc), "id": task["id"]},
    )
    for order in range(clips):
        clip = await create_clip(db, task_id=task["id"])
        await db.execute(
            text("UPDATE generated_clips SET clip_order = :o WHERE id = :id"),
            {"o": order + 10, "id": clip["id"]},
        )
        await db.commit()
    await db.commit()
    return task["id"]


async def _deliveries(db, user_id):
    result = await db.execute(
        text("SELECT id, event, url, payload, task_id FROM webhook_deliveries WHERE user_id = :u"),
        {"u": user_id},
    )
    return [dict(r) for r in result.mappings().all()]


async def test_save_creates_secret_once(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    first = await service.save(user_id, url=URL, enabled=True)
    assert first["secret"].startswith("whsec_")
    assert first["secret_hint"] == first["secret"][-4:]
    second = await service.save(user_id, url=URL, enabled=True)
    assert "secret" not in second
    assert second["secret_hint"] == first["secret"][-4:]
    assert "secret" not in await service.get_settings(user_id)


async def test_save_rejects_invalid_url(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    with pytest.raises(WebhookUrlError):
        await service.save(user_id, url="ftp://example.com/x", enabled=True)
    assert await service.get_settings(user_id) == {"configured": False}


async def test_secret_is_encrypted_at_rest(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    saved = await service.save(user_id, url=URL, enabled=True)
    raw = (
        await db_session.execute(
            text("SELECT secret_enc FROM user_webhooks WHERE user_id = :u"), {"u": user_id}
        )
    ).scalar_one()
    assert raw != saved["secret"]
    assert decrypt_setting_value(raw) == saved["secret"]


async def test_rotate_changes_secret(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    assert await service.rotate_secret(user_id) is None
    saved = await service.save(user_id, url=URL, enabled=True)
    new_secret = await service.rotate_secret(user_id)
    assert new_secret and new_secret != saved["secret"]
    assert (await service.get_settings(user_id))["secret_hint"] == new_secret[-4:]


async def test_delete(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    assert await service.delete(user_id) is False
    await service.save(user_id, url=URL, enabled=True)
    assert await service.delete(user_id) is True
    assert (await service.get_settings(user_id))["configured"] is False


async def test_enqueue_noop_without_config(db_session):
    user_id = await _user(db_session)
    task_id = await _completed_task(db_session, user_id)
    assert await WebhookService(db_session).enqueue_task_completed(task_id) is None
    assert await _deliveries(db_session, user_id) == []


async def test_enqueue_noop_when_disabled(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    await service.save(user_id, url=URL, enabled=False)
    task_id = await _completed_task(db_session, user_id)
    assert await service.enqueue_task_completed(task_id) is None


async def test_enqueue_builds_payload_from_db(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    await service.save(user_id, url=URL, enabled=True)
    task_id = await _completed_task(db_session, user_id, clips=2)
    delivery_id = await service.enqueue_task_completed(task_id)
    rows = await _deliveries(db_session, user_id)
    assert len(rows) == 1
    assert rows[0]["id"] == delivery_id
    assert rows[0]["event"] == "task.completed"
    assert rows[0]["url"] == URL
    assert len(rows[0]["payload"]["clips"]) == 2
    assert rows[0]["payload"]["task"]["id"] == task_id


async def test_enqueue_twice_inserts_once(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    await service.save(user_id, url=URL, enabled=True)
    task_id = await _completed_task(db_session, user_id)
    assert await service.enqueue_task_completed(task_id) is not None
    assert await service.enqueue_task_completed(task_id) is None
    assert len(await _deliveries(db_session, user_id)) == 1


async def test_reconcile_picks_up_missed_completion(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    await service.save(user_id, url=URL, enabled=True)
    task_id = await _completed_task(db_session, user_id, clips=1)
    assert await service.reconcile() == 1
    assert await service.reconcile() == 0
    rows = await _deliveries(db_session, user_id)
    assert [r["task_id"] for r in rows] == [task_id]


async def test_send_test_inserts_webhook_test_event(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    assert await service.send_test(user_id) is None
    await service.save(user_id, url=URL, enabled=False)
    assert await service.send_test(user_id) is None
    await service.save(user_id, url=URL, enabled=True)
    delivery_id = await service.send_test(user_id)
    rows = await _deliveries(db_session, user_id)
    assert len(rows) == 1
    assert rows[0]["id"] == delivery_id
    assert rows[0]["event"] == "webhook.test"
    assert rows[0]["task_id"] is None


async def test_unreadable_secret_does_not_break_settings(db_session):
    user_id = await _user(db_session)
    service = WebhookService(db_session)
    await service.save(user_id, url=URL, enabled=True)
    await db_session.execute(
        text("UPDATE user_webhooks SET secret_enc = 'garbage' WHERE user_id = :u"), {"u": user_id}
    )
    await db_session.commit()
    settings = await service.get_settings(user_id)
    assert settings["configured"] is True
    assert settings["secret_hint"] is None
    assert settings["secret_unreadable"] is True
    assert await service.rotate_secret(user_id) is not None
    assert "secret_unreadable" not in await service.get_settings(user_id)


async def test_save_checks_address_when_private_targets_off(db_session):
    from types import SimpleNamespace

    from src.config import get_config

    config = get_config()
    off = SimpleNamespace(**{**vars(config), "webhook_allow_private_targets": False}) \
        if hasattr(config, "__dict__") else None
    service = WebhookService(db_session, off)
    user_id = await _user(db_session)
    with pytest.raises(WebhookUrlError):
        await service.save(user_id, url="https://127.0.0.1/hook", enabled=True)
    assert await service.get_settings(user_id) == {"configured": False}
