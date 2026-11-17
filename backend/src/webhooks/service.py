"""Webhook service - settings, secret handling and the delivery queue's producers.

Commits its own writes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Config, get_config
from ..repositories.webhook_repository import WebhookRepository
from ..runtime_settings import decrypt_setting_value, encrypt_setting_value
from .payload import build_task_completed_payload, build_test_payload
from .signing import generate_secret
from .url_safety import resolve_target, validate_webhook_url

logger = logging.getLogger(__name__)

TASK_COMPLETED = "task.completed"
WEBHOOK_TEST = "webhook.test"
_HINT_LENGTH = 4


class WebhookService:
    def __init__(self, db: AsyncSession, config: Optional[Config] = None):
        self.db = db
        self.config = config or get_config()
        self.repo = WebhookRepository

    def _hint(self, secret_enc: str) -> Optional[str]:
        try:
            return decrypt_setting_value(secret_enc)[-_HINT_LENGTH:]
        except Exception:
            logger.warning("Webhook secret could not be decrypted")
            return None

    async def get_settings(self, user_id: str) -> Dict[str, Any]:
        cfg = await self.repo.get_config(self.db, user_id)
        if cfg is None:
            return {"configured": False}
        hint = self._hint(cfg["secret_enc"])
        settings: Dict[str, Any] = {
            "configured": True,
            "url": cfg["url"],
            "enabled": cfg["enabled"],
            "secret_hint": hint,
            "created_at": cfg["created_at"],
            "updated_at": cfg["updated_at"],
            "last_delivery": await self.repo.last_delivery(self.db, user_id),
        }
        if hint is None:
            settings["secret_unreadable"] = True
        return settings

    async def save(self, user_id: str, *, url: str, enabled: bool) -> Dict[str, Any]:
        allow_private = self.config.webhook_allow_private_targets
        clean_url = validate_webhook_url(url, allow_private=allow_private)
        if not allow_private:
            # Blocked addresses are refused now, not only at the first send.
            await resolve_target(clean_url, allow_private=False)
        existing = await self.repo.get_config(self.db, user_id)
        secret: Optional[str] = None
        secret_enc: Optional[str] = None
        if existing is None:
            secret = generate_secret()
            secret_enc = encrypt_setting_value(secret)
        await self.repo.upsert_config(
            self.db, user_id, url=clean_url, enabled=enabled, secret_enc=secret_enc
        )
        await self.db.commit()
        settings = await self.get_settings(user_id)
        if secret is not None:
            settings["secret"] = secret
        return settings

    async def rotate_secret(self, user_id: str) -> Optional[str]:
        secret = generate_secret()
        updated = await self.repo.set_secret(self.db, user_id, encrypt_setting_value(secret))
        await self.db.commit()
        return secret if updated else None

    async def delete(self, user_id: str) -> bool:
        deleted = await self.repo.delete_config(self.db, user_id)
        await self.db.commit()
        return deleted

    async def send_test(self, user_id: str) -> Optional[str]:
        cfg = await self.repo.get_config(self.db, user_id)
        if cfg is None or not cfg["enabled"]:
            return None
        delivery_id = str(uuid4())
        payload = build_test_payload(
            delivery_id=delivery_id, created_at=datetime.now(timezone.utc)
        )
        await self.repo.insert_delivery(
            self.db,
            delivery_id=delivery_id,
            user_id=user_id,
            task_id=None,
            event=WEBHOOK_TEST,
            url=cfg["url"],
            payload=payload,
            task_completed_at=None,
        )
        await self.db.commit()
        return delivery_id

    async def enqueue_task_completed(self, task_id: str) -> Optional[str]:
        """Queue the task.completed delivery; idempotent per (task, completed_at)."""
        task = await self.repo.get_task_for_payload(self.db, task_id)
        if task is None or task["status"] != "completed" or task["completed_at"] is None:
            return None
        cfg = await self.repo.get_config(self.db, task["user_id"])
        if cfg is None or not cfg["enabled"]:
            return None
        if task["completed_at"] < cfg["enabled_since"]:
            return None

        clips = await self.repo.get_clips_for_payload(self.db, task_id)
        delivery_id = str(uuid4())
        payload = build_task_completed_payload(
            delivery_id=delivery_id,
            created_at=datetime.now(timezone.utc),
            task=task,
            clips=clips,
            public_api_url=self.config.public_api_url,
            app_base_url=self.config.app_base_url,
        )
        inserted = await self.repo.insert_delivery(
            self.db,
            delivery_id=delivery_id,
            user_id=task["user_id"],
            task_id=task_id,
            event=TASK_COMPLETED,
            url=cfg["url"],
            payload=payload,
            task_completed_at=task["completed_at"],
        )
        await self.db.commit()
        return delivery_id if inserted else None

    async def reconcile(self) -> int:
        """Enqueue completions the fast path missed. Returns how many were inserted."""
        rows = await self.repo.find_unannounced_completions(self.db)
        count = 0
        for row in rows:
            try:
                if await self.enqueue_task_completed(row["task_id"]):
                    count += 1
            except Exception:
                await self.db.rollback()
                logger.exception("webhook reconcile failed for task %s", row["task_id"])
        return count
