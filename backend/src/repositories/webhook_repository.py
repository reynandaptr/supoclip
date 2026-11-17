"""
Webhook repository - the user's webhook endpoint and its delivery queue.

No method commits; callers own the transaction.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_CONFIG_COLUMNS = "user_id, url, secret_enc, enabled, enabled_since, created_at, updated_at"


def _secs(delta: timedelta) -> float:
    return delta.total_seconds()


class WebhookRepository:
    """Repository for `user_webhooks` and `webhook_deliveries`."""

    @staticmethod
    async def get_config(db: AsyncSession, user_id: str) -> Optional[Dict[str, Any]]:
        result = await db.execute(
            text(f"SELECT {_CONFIG_COLUMNS} FROM user_webhooks WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    async def upsert_config(
        db: AsyncSession,
        user_id: str,
        *,
        url: str,
        enabled: bool,
        secret_enc: Optional[str],
    ) -> Dict[str, Any]:
        """Create the config (secret required) or update url/enabled (None keeps the secret)."""
        existing = await WebhookRepository.get_config(db, user_id)
        if existing is None:
            if secret_enc is None:
                raise ValueError("secret_enc is required when creating a webhook")
            result = await db.execute(
                text(
                    f"""
                    INSERT INTO user_webhooks (user_id, url, secret_enc, enabled)
                    VALUES (:user_id, :url, :secret_enc, :enabled)
                    RETURNING {_CONFIG_COLUMNS}
                    """
                ),
                {"user_id": user_id, "url": url, "secret_enc": secret_enc, "enabled": enabled},
            )
            return dict(result.mappings().first())

        result = await db.execute(
            text(
                f"""
                UPDATE user_webhooks
                SET url = :url,
                    enabled = :enabled,
                    secret_enc = COALESCE(:secret_enc, secret_enc),
                    enabled_since = CASE WHEN :enabled AND NOT enabled THEN now() ELSE enabled_since END,
                    updated_at = now()
                WHERE user_id = :user_id
                RETURNING {_CONFIG_COLUMNS}
                """
            ),
            {"user_id": user_id, "url": url, "secret_enc": secret_enc, "enabled": enabled},
        )
        if existing["enabled"] and not enabled:
            await WebhookRepository.cancel_pending(db, user_id)
        return dict(result.mappings().first())

    @staticmethod
    async def set_secret(db: AsyncSession, user_id: str, secret_enc: str) -> bool:
        result = await db.execute(
            text(
                "UPDATE user_webhooks SET secret_enc = :secret_enc, updated_at = now() "
                "WHERE user_id = :user_id"
            ),
            {"user_id": user_id, "secret_enc": secret_enc},
        )
        return result.rowcount > 0

    @staticmethod
    async def delete_config(db: AsyncSession, user_id: str) -> bool:
        await WebhookRepository.cancel_pending(db, user_id)
        result = await db.execute(
            text("DELETE FROM user_webhooks WHERE user_id = :user_id"), {"user_id": user_id}
        )
        return result.rowcount > 0

    @staticmethod
    async def cancel_pending(db: AsyncSession, user_id: str) -> int:
        result = await db.execute(
            text(
                "UPDATE webhook_deliveries SET status = 'cancelled' "
                "WHERE user_id = :user_id AND status = 'pending'"
            ),
            {"user_id": user_id},
        )
        return result.rowcount

    @staticmethod
    async def insert_delivery(
        db: AsyncSession,
        *,
        delivery_id: str,
        user_id: str,
        task_id: Optional[str],
        event: str,
        url: str,
        payload: Dict[str, Any],
        task_completed_at: Optional[datetime],
    ) -> bool:
        """Queue a delivery. False when this completion already has one."""
        result = await db.execute(
            text(
                """
                INSERT INTO webhook_deliveries
                    (id, user_id, task_id, event, url, payload, task_completed_at)
                VALUES
                    (:id, :user_id, :task_id, :event, :url, CAST(:payload AS JSONB), :task_completed_at)
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "id": delivery_id,
                "user_id": user_id,
                "task_id": task_id,
                "event": event,
                "url": url,
                "payload": json.dumps(payload),
                "task_completed_at": task_completed_at,
            },
        )
        return result.rowcount > 0

    @staticmethod
    async def get_task_for_payload(db: AsyncSession, task_id: str) -> Optional[Dict[str, Any]]:
        result = await db.execute(
            text(
                """
                SELECT t.id, t.user_id, t.status, t.workflow, t.completed_at,
                       s.title AS source_title, s.url AS source_url, s.type AS source_type
                FROM tasks t
                LEFT JOIN sources s ON s.id = t.source_id
                WHERE t.id = :task_id
                """
            ),
            {"task_id": task_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    async def get_clips_for_payload(db: AsyncSession, task_id: str) -> List[Dict[str, Any]]:
        result = await db.execute(
            text(
                """
                SELECT id, clip_order, hook_title, text, start_time, end_time, duration,
                       virality_score, workflow_role, part_number
                FROM generated_clips
                WHERE task_id = :task_id
                ORDER BY clip_order, created_at
                """
            ),
            {"task_id": task_id},
        )
        return [dict(r) for r in result.mappings().all()]

    @staticmethod
    async def find_unannounced_completions(
        db: AsyncSession,
        *,
        window: timedelta = timedelta(hours=24),
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Completed tasks of enabled webhooks that have no delivery yet."""
        result = await db.execute(
            text(
                """
                SELECT t.id AS task_id, t.user_id, t.completed_at
                FROM tasks t
                JOIN user_webhooks w ON w.user_id = t.user_id AND w.enabled
                WHERE t.status = 'completed'
                  AND t.completed_at IS NOT NULL
                  AND t.completed_at > now() - make_interval(secs => :window_secs)
                  AND t.completed_at >= w.enabled_since
                  AND NOT EXISTS (
                      SELECT 1 FROM webhook_deliveries d
                      WHERE d.task_id = t.id
                        AND d.event = 'task.completed'
                        AND d.task_completed_at = t.completed_at
                  )
                ORDER BY t.completed_at
                LIMIT :limit
                """
            ),
            {"window_secs": _secs(window), "limit": limit},
        )
        return [dict(r) for r in result.mappings().all()]

    @staticmethod
    async def claim_due(
        db: AsyncSession,
        *,
        limit: int = 20,
        lease: timedelta = timedelta(minutes=2),
    ) -> List[Dict[str, Any]]:
        """Claim due deliveries and push their next attempt out by `lease`."""
        result = await db.execute(
            text(
                """
                UPDATE webhook_deliveries d
                SET next_attempt_at = now() + make_interval(secs => :lease_secs)
                FROM (
                    SELECT id FROM webhook_deliveries
                    WHERE status = 'pending' AND next_attempt_at <= now()
                    ORDER BY next_attempt_at
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                ) due
                WHERE d.id = due.id
                RETURNING d.id, d.user_id, d.event, d.url, d.payload, d.attempts
                """
            ),
            {"lease_secs": _secs(lease), "limit": limit},
        )
        return [dict(r) for r in result.mappings().all()]

    @staticmethod
    async def mark_delivered(db: AsyncSession, delivery_id: str, *, status_code: int) -> None:
        await db.execute(
            text(
                """
                UPDATE webhook_deliveries
                SET status = 'delivered', attempts = attempts + 1, last_status_code = :status_code,
                    last_error = NULL, delivered_at = now()
                WHERE id = :id AND status = 'pending'
                """
            ),
            {"id": delivery_id, "status_code": status_code},
        )

    @staticmethod
    async def mark_attempt_failed(
        db: AsyncSession,
        delivery_id: str,
        *,
        attempts: int,
        status_code: Optional[int],
        error: str,
        retry_at: Optional[datetime],
    ) -> None:
        """Record a failed attempt; no `retry_at` means no retries are left."""
        await db.execute(
            text(
                """
                UPDATE webhook_deliveries
                SET attempts = :attempts,
                    last_status_code = :status_code,
                    last_error = :error,
                    status = CASE WHEN CAST(:retry_at AS TIMESTAMPTZ) IS NULL THEN 'failed' ELSE 'pending' END,
                    next_attempt_at = COALESCE(CAST(:retry_at AS TIMESTAMPTZ), next_attempt_at)
                WHERE id = :id AND status = 'pending'
                """
            ),
            {
                "id": delivery_id,
                "attempts": attempts,
                "status_code": status_code,
                "error": (error or "")[:500],
                "retry_at": retry_at,
            },
        )

    @staticmethod
    async def prune(db: AsyncSession, *, older_than: timedelta = timedelta(days=30)) -> int:
        result = await db.execute(
            text(
                """
                DELETE FROM webhook_deliveries
                WHERE status IN ('delivered', 'failed', 'cancelled')
                  AND created_at < now() - make_interval(secs => :secs)
                """
            ),
            {"secs": _secs(older_than)},
        )
        return result.rowcount

    @staticmethod
    async def mark_cancelled(db: AsyncSession, delivery_id: str) -> None:
        await db.execute(
            text("UPDATE webhook_deliveries SET status = 'cancelled' WHERE id = :id AND status = 'pending'"),
            {"id": delivery_id},
        )

    @staticmethod
    async def last_delivery(db: AsyncSession, user_id: str) -> Optional[Dict[str, Any]]:
        result = await db.execute(
            text(
                """
                SELECT status, attempts, last_status_code, created_at
                FROM webhook_deliveries
                WHERE user_id = :user_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None
