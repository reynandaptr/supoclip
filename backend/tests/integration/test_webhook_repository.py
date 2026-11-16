from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.repositories.webhook_repository import WebhookRepository
from tests.fixtures.factories import (
    create_clip,
    create_source,
    create_task,
    create_user,
)

pytestmark = pytest.mark.asyncio

EVENT = "task.completed"


@pytest.fixture(autouse=True)
async def _clean_webhook_tables(db_session):
    # The test database persists between tests; queue-wide queries need an empty start.
    await db_session.execute(text("DELETE FROM webhook_deliveries"))
    await db_session.execute(text("DELETE FROM user_webhooks"))
    await db_session.commit()


async def _user(db):
    return (await create_user(db))["id"]


async def _config(db, user_id, enabled=True):
    cfg = await WebhookRepository.upsert_config(
        db, user_id, url="https://example.com/hook", enabled=enabled, secret_enc="enc"
    )
    await db.commit()
    return cfg


async def _delivery(db, user_id, task_id=None, completed_at=None, event=EVENT):
    delivery_id = str(uuid4())
    inserted = await WebhookRepository.insert_delivery(
        db,
        delivery_id=delivery_id,
        user_id=user_id,
        task_id=task_id,
        event=event,
        url="https://example.com/hook",
        payload={"a": 1},
        task_completed_at=completed_at,
    )
    await db.commit()
    return delivery_id, inserted


async def _status(db, delivery_id):
    row = await db.execute(
        text("SELECT status FROM webhook_deliveries WHERE id = :id"), {"id": delivery_id}
    )
    return row.scalar_one()


async def _completed_task(db, user_id, completed_at):
    source = await create_source(db)
    task = await create_task(db, user_id=user_id, source_id=source["id"])
    await db.execute(
        text("UPDATE tasks SET completed_at = :c WHERE id = :id"),
        {"c": completed_at, "id": task["id"]},
    )
    await db.commit()
    return task["id"]


async def test_upsert_then_disable_cancels_pending(db_session):
    user_id = await _user(db_session)
    await _config(db_session, user_id)
    delivery_id, _ = await _delivery(db_session, user_id)

    cfg = await WebhookRepository.upsert_config(
        db_session, user_id, url="https://example.com/hook", enabled=False, secret_enc=None
    )
    await db_session.commit()

    assert cfg["enabled"] is False
    assert cfg["secret_enc"] == "enc"
    assert await _status(db_session, delivery_id) == "cancelled"


async def test_reenable_moves_enabled_since(db_session):
    user_id = await _user(db_session)
    first = await _config(db_session, user_id)
    await WebhookRepository.upsert_config(
        db_session, user_id, url=first["url"], enabled=False, secret_enc=None
    )
    await db_session.commit()
    again = await WebhookRepository.upsert_config(
        db_session, user_id, url=first["url"], enabled=True, secret_enc=None
    )
    await db_session.commit()

    assert again["enabled_since"] > first["enabled_since"]


async def test_delete_cancels_pending(db_session):
    user_id = await _user(db_session)
    await _config(db_session, user_id)
    delivery_id, _ = await _delivery(db_session, user_id)

    assert await WebhookRepository.delete_config(db_session, user_id) is True
    await db_session.commit()

    assert await WebhookRepository.get_config(db_session, user_id) is None
    assert await _status(db_session, delivery_id) == "cancelled"


async def test_insert_delivery_is_idempotent_per_completion(db_session):
    user_id = await _user(db_session)
    source = await create_source(db_session)
    task = await create_task(db_session, user_id=user_id, source_id=source["id"])
    done = datetime.now(timezone.utc)

    _, first = await _delivery(db_session, user_id, task["id"], done)
    _, second = await _delivery(db_session, user_id, task["id"], done)
    _, third = await _delivery(db_session, user_id, task["id"], done + timedelta(minutes=1))

    assert (first, second, third) == (True, False, True)


async def test_reconciler_skips_completions_before_enabled_since(db_session):
    user_id = await _user(db_session)
    old_task = await _completed_task(
        db_session, user_id, datetime.now(timezone.utc) - timedelta(hours=1)
    )
    await _config(db_session, user_id)

    assert await WebhookRepository.find_unannounced_completions(db_session) == []

    new_done = datetime.now(timezone.utc) + timedelta(seconds=1)
    new_task = await _completed_task(db_session, user_id, new_done)
    found = await WebhookRepository.find_unannounced_completions(db_session)
    assert [r["task_id"] for r in found] == [new_task]
    assert old_task not in [r["task_id"] for r in found]
    assert found[0]["user_id"] == user_id

    await _delivery(db_session, user_id, new_task, found[0]["completed_at"])
    assert await WebhookRepository.find_unannounced_completions(db_session) == []


async def test_reconciler_ignores_disabled_and_old(db_session):
    disabled_user = await _user(db_session)
    await _config(db_session, disabled_user, enabled=False)
    await _completed_task(
        db_session, disabled_user, datetime.now(timezone.utc) + timedelta(seconds=1)
    )

    old_user = await _user(db_session)
    await _config(db_session, old_user)
    await db_session.execute(
        text("UPDATE user_webhooks SET enabled_since = now() - interval '48 hours' WHERE user_id = :u"),
        {"u": old_user},
    )
    await _completed_task(
        db_session, old_user, datetime.now(timezone.utc) - timedelta(hours=25)
    )
    await db_session.commit()

    assert await WebhookRepository.find_unannounced_completions(db_session) == []


async def test_claim_due_leases_rows(db_session):
    user_id = await _user(db_session)
    delivery_id, _ = await _delivery(db_session, user_id)

    first = await WebhookRepository.claim_due(db_session)
    await db_session.commit()
    second = await WebhookRepository.claim_due(db_session)
    await db_session.commit()

    assert [r["id"] for r in first] == [delivery_id]
    assert first[0]["payload"] == {"a": 1}
    assert first[0]["attempts"] == 0
    assert second == []


async def test_mark_attempt_failed_terminal(db_session):
    user_id = await _user(db_session)
    delivery_id, _ = await _delivery(db_session, user_id)

    await WebhookRepository.mark_attempt_failed(
        db_session,
        delivery_id,
        attempts=8,
        status_code=500,
        error="x" * 900,
        retry_at=None,
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            text("SELECT status, attempts, last_status_code, last_error FROM webhook_deliveries WHERE id = :id"),
            {"id": delivery_id},
        )
    ).one()
    assert row.status == "failed"
    assert row.attempts == 8
    assert row.last_status_code == 500
    assert len(row.last_error) <= 500


async def test_mark_delivered_cancelled_prune_and_last_delivery(db_session):
    user_id = await _user(db_session)
    done_id, _ = await _delivery(db_session, user_id)
    await WebhookRepository.mark_delivered(db_session, done_id, status_code=204)
    cancelled_id, _ = await _delivery(db_session, user_id)
    await WebhookRepository.mark_cancelled(db_session, cancelled_id)
    await db_session.commit()

    assert await _status(db_session, done_id) == "delivered"
    assert await _status(db_session, cancelled_id) == "cancelled"
    last = await WebhookRepository.last_delivery(db_session, user_id)
    assert last["status"] in {"delivered", "cancelled"}

    await db_session.execute(
        text("UPDATE webhook_deliveries SET created_at = now() - interval '40 days' WHERE id = :id"),
        {"id": done_id},
    )
    await db_session.commit()
    assert await WebhookRepository.prune(db_session) == 1


async def test_payload_queries(db_session):
    user_id = await _user(db_session)
    source = await create_source(db_session, title="My Video", source_type="youtube", url="https://y/1")
    task = await create_task(db_session, user_id=user_id, source_id=source["id"])
    clip_a = await create_clip(db_session, task_id=task["id"], text_value="A")
    await db_session.execute(
        text("UPDATE generated_clips SET clip_order = 2 WHERE id = :id"), {"id": clip_a["id"]}
    )
    await db_session.commit()
    clip_b = await create_clip(db_session, task_id=task["id"], text_value="B")

    info = await WebhookRepository.get_task_for_payload(db_session, task["id"])
    assert info["user_id"] == user_id
    assert info["source_title"] == "My Video"
    assert info["source_url"] == "https://y/1"
    assert info["source_type"] == "youtube"
    assert info["workflow"] == "highlights"

    clips = await WebhookRepository.get_clips_for_payload(db_session, task["id"])
    assert [c["id"] for c in clips] == [clip_b["id"], clip_a["id"]]
    assert set(clips[0]) == {
        "id", "clip_order", "hook_title", "text", "start_time", "end_time",
        "duration", "virality_score", "workflow_role", "part_number",
    }
    assert await WebhookRepository.get_task_for_payload(db_session, str(uuid4())) is None


async def test_cancelled_inflight_delivery_is_not_resurrected(db_session):
    user_id = await _user(db_session)
    await _config(db_session, user_id)
    delivery_id, _ = await _delivery(db_session, user_id)

    claimed = await WebhookRepository.claim_due(db_session)
    assert [r["id"] for r in claimed] == [delivery_id]
    await WebhookRepository.cancel_pending(db_session, user_id)
    await db_session.commit()

    await WebhookRepository.mark_attempt_failed(
        db_session,
        delivery_id,
        attempts=1,
        status_code=500,
        error="boom",
        retry_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    await db_session.commit()
    assert await _status(db_session, delivery_id) == "cancelled"

    await WebhookRepository.mark_delivered(db_session, delivery_id, status_code=200)
    await db_session.commit()
    assert await _status(db_session, delivery_id) == "cancelled"
