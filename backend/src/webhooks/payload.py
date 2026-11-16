"""JSON bodies for outgoing webhook events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def build_task_completed_payload(
    *,
    delivery_id: str,
    created_at: datetime,
    task: Mapping[str, Any],
    clips: Sequence[Mapping[str, Any]],
    public_api_url: str,
    app_base_url: str,
) -> dict:
    api = public_api_url.rstrip("/")
    app = app_base_url.rstrip("/")
    task_id = task["id"]
    return {
        "event": "task.completed",
        "delivery_id": delivery_id,
        "created_at": _iso(created_at),
        "task": {
            "id": task_id,
            "status": task["status"],
            "source_title": task.get("source_title"),
            "source_url": task.get("source_url"),
            "source_type": task.get("source_type"),
            "workflow": task.get("workflow"),
            "completed_at": _iso(task.get("completed_at")),
            "clips_count": len(clips),
            "url": f"{app}/tasks/{task_id}",
        },
        "clips": [
            {
                "id": clip["id"],
                "order": clip["clip_order"],
                "hook_title": clip.get("hook_title"),
                "text": clip.get("text"),
                "start_time": clip.get("start_time"),
                "end_time": clip.get("end_time"),
                "duration": clip.get("duration"),
                "virality_score": clip.get("virality_score"),
                "workflow_role": clip.get("workflow_role"),
                "part_number": clip.get("part_number"),
                "download_url": f"{api}/tasks/{task_id}/clips/{clip['id']}/file?download=true",
            }
            for clip in clips
        ],
    }


def build_test_payload(*, delivery_id: str, created_at: datetime) -> dict:
    return {
        "event": "webhook.test",
        "delivery_id": delivery_id,
        "created_at": _iso(created_at),
        "message": "SupoClip webhook test",
    }
