"""Send queued webhook deliveries: the ``deliver_webhooks`` cron.

Every run first enqueues completions the fast path missed, then claims due rows, posts each one
signed with the user's current secret, and records the outcome. A row that fails is retried on
``BACKOFF_SECONDS``; after the last delay it is marked ``failed``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from ..config import get_config
from ..database import AsyncSessionLocal
from ..repositories.webhook_repository import WebhookRepository
from ..runtime_settings import decrypt_setting_value
from .service import WebhookService
from .signing import delivery_headers
from .url_safety import WebhookUrlError, resolve_target

logger = logging.getLogger(__name__)

# Delay after failed attempt n (n = 1..7); the 8th failure is final.
BACKOFF_SECONDS = (60, 300, 900, 3600, 10800, 21600, 43200)
REQUEST_TIMEOUT_SECONDS = 10
CLAIM_LIMIT = 20
MAX_CONCURRENT_SENDS = 5
SECRET_UNREADABLE = "webhook secret unreadable"


def next_retry_delay(attempts: int) -> int | None:
    """Seconds until the next try after `attempts` attempts (this one included); None = give up."""
    if attempts > len(BACKOFF_SECONDS):
        return None
    return BACKOFF_SECONDS[max(attempts, 1) - 1]


@dataclass
class DeliveryResult:
    ok: bool
    status_code: int | None
    error: str | None


def _body(payload: Any) -> bytes:
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    return json.dumps(payload, separators=(",", ":"), default=str).encode()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        detail = str(exc)
        return f"timed out: {detail}" if detail else "timed out"
    return str(exc) or type(exc).__name__


async def send_delivery(
    row: Mapping[str, Any],
    *,
    secret: str,
    client: httpx.AsyncClient,
    allow_private: bool,
) -> DeliveryResult:
    """POST one delivery to the address its host resolves to now (checked again before every send)."""
    try:
        target = await resolve_target(row["url"], allow_private=allow_private)
    except WebhookUrlError as exc:
        return DeliveryResult(False, None, str(exc))

    body = _body(row["payload"])
    headers = delivery_headers(
        secret=secret,
        event=row["event"],
        delivery_id=row["id"],
        timestamp=int(time.time()),
        body=body,
    )
    try:
        original = httpx.URL(target.url)
        # Connect to the address that was just checked, so a second lookup can't move us elsewhere;
        # the receiver still sees its own host name, and TLS verifies that name.
        headers["Host"] = original.netloc.decode("ascii")
        extensions = {"sni_hostname": target.host} if original.scheme == "https" else {}
        request = client.build_request(
            "POST",
            original.copy_with(host=target.ip),
            content=body,
            headers=headers,
            extensions=extensions,
        )
        response = await asyncio.wait_for(
            client.send(request, follow_redirects=False, stream=True),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, httpx.InvalidURL, asyncio.TimeoutError) as exc:
        return DeliveryResult(False, None, _describe(exc))
    # The response body is never read: only the status matters.
    await response.aclose()

    if 200 <= response.status_code < 300:
        return DeliveryResult(True, response.status_code, None)
    return DeliveryResult(False, response.status_code, f"HTTP {response.status_code}")


async def _current_secrets(db, user_ids: set[str]) -> dict[str, Optional[str]]:
    """user_id -> decrypted secret, "" when it can't be decrypted; users without an enabled webhook are absent."""
    secrets: dict[str, Optional[str]] = {}
    for user_id in user_ids:
        cfg = await WebhookRepository.get_config(db, user_id)
        if cfg is None or not cfg["enabled"]:
            continue
        try:
            secrets[user_id] = decrypt_setting_value(cfg["secret_enc"])
        except Exception:
            logger.warning("Webhook secret for user %s could not be decrypted", user_id)
            secrets[user_id] = ""
    return secrets


async def _record_failure(db, row: Mapping[str, Any], result: DeliveryResult) -> None:
    attempts = int(row["attempts"]) + 1
    delay = next_retry_delay(attempts)
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay) if delay is not None else None
    await WebhookRepository.mark_attempt_failed(
        db,
        row["id"],
        attempts=attempts,
        status_code=result.status_code,
        error=result.error or "delivery failed",
        retry_at=retry_at,
    )
    if retry_at is None:
        logger.warning("Webhook delivery %s failed after %s attempts: %s", row["id"], attempts, result.error)


async def deliver_webhooks(ctx: dict) -> dict:
    config = get_config()
    summary = {"reconciled": 0, "sent": 0, "delivered": 0, "failed": 0, "pruned": 0}

    # Its own session, so a reconcile problem never holds up sending.
    try:
        async with AsyncSessionLocal() as db:
            summary["reconciled"] = await WebhookService(db, config).reconcile()
    except Exception:
        logger.exception("Webhook reconcile failed")

    async with AsyncSessionLocal() as db:
        rows = await WebhookRepository.claim_due(db, limit=CLAIM_LIMIT)
        await db.commit()

        secrets = await _current_secrets(db, {row["user_id"] for row in rows}) if rows else {}
        sendable = []
        unreadable = []
        for row in rows:
            secret = secrets.get(row["user_id"])
            if secret is None:
                # Webhook removed or switched off since this was queued: never send it unsigned.
                await WebhookRepository.mark_cancelled(db, row["id"])
            elif secret == "":
                unreadable.append(row)
            else:
                sendable.append((row, secret))
        await db.commit()

        results: list[DeliveryResult] = []
        if sendable:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_SENDS)
            allow_private = config.webhook_allow_private_targets

            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
                # Pinned connections are keyed by IP; never reuse one for another host name.
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as client:

                async def send_one(row: Mapping[str, Any], secret: str) -> DeliveryResult:
                    async with semaphore:
                        try:
                            return await send_delivery(
                                row, secret=secret, client=client, allow_private=allow_private
                            )
                        except Exception as exc:
                            logger.exception("Webhook delivery %s raised", row["id"])
                            return DeliveryResult(False, None, _describe(exc))

                results = await asyncio.gather(*(send_one(row, secret) for row, secret in sendable))
        summary["sent"] = len(sendable)

        for (row, _secret), result in zip(sendable, results):
            if result.ok:
                await WebhookRepository.mark_delivered(db, row["id"], status_code=result.status_code)
                summary["delivered"] += 1
            else:
                await _record_failure(db, row, result)
                summary["failed"] += 1
        for row in unreadable:
            await _record_failure(db, row, DeliveryResult(False, None, SECRET_UNREADABLE))
            summary["failed"] += 1

        summary["pruned"] = await WebhookRepository.prune(db)
        await db.commit()

    if any(summary.values()):
        logger.info("Webhook deliveries: %s", summary)
    return summary
