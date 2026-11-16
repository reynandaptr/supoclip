"""process_task enqueues the task.completed webhook; a webhook problem never fails the task."""

import pytest
from sqlalchemy import text

from src.repositories.clip_repository import ClipRepository
from src.repositories.task_repository import TaskRepository
from src.webhooks.service import WebhookService
from tests.integration.test_clip_edit_atomicity import make_task
from tests.integration.test_processing_cancellation import setup_worker


@pytest.fixture(autouse=True)
async def _clean_webhook_tables(db_session, monkeypatch):
    monkeypatch.setenv("MEDIA_STORAGE", "local")
    # The test database persists between tests.
    await db_session.execute(text("DELETE FROM webhook_deliveries"))
    await db_session.execute(text("DELETE FROM user_webhooks"))
    await db_session.commit()


async def _deliveries(db, task_id):
    rows = await db.execute(
        text("SELECT event, payload FROM webhook_deliveries WHERE task_id = :t"), {"t": task_id}
    )
    return rows.all()


async def test_completion_enqueues_one_delivery(db_session, tmp_path):
    task_id, _ids, original = await make_task(db_session, tmp_path, count=0, status="queued")
    task = await TaskRepository.get_task_by_id(db_session, task_id)
    await WebhookService(db_session).save(task["user_id"], url="https://example.com/hook", enabled=True)
    # process_task stamps a naive utcnow(); a database session zone other than UTC would shift it
    # before enabled_since (set by save). Backdate the opt-in so the test checks wiring, not zones.
    await db_session.execute(text("UPDATE user_webhooks SET enabled_since = now() - interval '1 day'"))
    await db_session.commit()
    worker, _path, _info = setup_worker(db_session, tmp_path, original)

    await worker.process_task(task_id, "upload://input.mp4", "upload")

    assert (await TaskRepository.get_task_by_id(db_session, task_id))["status"] == "completed"
    rows = await _deliveries(db_session, task_id)
    assert len(rows) == 1
    event, payload = rows[0]
    assert event == "task.completed"
    clips = await ClipRepository.get_clips_by_task(db_session, task_id)
    assert clips
    assert [c["id"] for c in payload["clips"]] == [c["id"] for c in clips]


async def test_enqueue_error_does_not_fail_task(db_session, tmp_path, monkeypatch):
    task_id, _ids, original = await make_task(db_session, tmp_path, count=0, status="queued")
    worker, _path, _info = setup_worker(db_session, tmp_path, original)

    async def boom(self, task_id):
        raise RuntimeError("webhook exploded")

    monkeypatch.setattr(WebhookService, "enqueue_task_completed", boom)

    await worker.process_task(task_id, "upload://input.mp4", "upload")

    assert (await TaskRepository.get_task_by_id(db_session, task_id))["status"] == "completed"
    worker._send_completion_notification_if_needed.assert_awaited_once()
