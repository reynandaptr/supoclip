"""MANUAL_CLIP_EDITS_ENABLED switches off the routes that encode video inside the API process."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from src.api.routes import tasks
from src.config import Config
from src.database import get_db

DISABLED_DETAIL = "Manual clip edits are disabled on this server."

EDIT_ROUTES = [
    ("PATCH", "/tasks/t/clips/c", {"start_offset": 0, "end_offset": 1}),
    ("POST", "/tasks/t/clips/c/split", {"split_time": 1}),
    ("POST", "/tasks/t/clips/merge", {"clip_ids": ["a", "b"]}),
    ("PATCH", "/tasks/t/clips/c/captions", {"caption_text": "hi"}),
    ("POST", "/tasks/t/clips/c/regenerate", {"start_offset": 0, "end_offset": 1}),
    ("GET", "/tasks/t/clips/c/export?preset=tiktok", None),
]


class RecordingTaskService:
    """Stands in for TaskService; every attribute access is recorded as a call."""

    calls: list[str] = []

    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        RecordingTaskService.calls.append(name)
        return AsyncMock(return_value={})


@pytest.fixture
async def http(monkeypatch):
    RecordingTaskService.calls = []
    monkeypatch.setattr(tasks, "TaskService", RecordingTaskService)
    monkeypatch.setattr(tasks, "_require_task_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(tasks, "_get_user_id_from_headers", AsyncMock(return_value="owner"))
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace(close=AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _call(http, method, path, body):
    if body is None:
        return await http.request(method, path)
    return await http.request(method, path, json=body)


def test_config_default_enables_manual_edits(monkeypatch):
    monkeypatch.delenv("MANUAL_CLIP_EDITS_ENABLED", raising=False)
    assert Config().manual_clip_edits_enabled is True
    monkeypatch.setenv("MANUAL_CLIP_EDITS_ENABLED", "false")
    assert Config().manual_clip_edits_enabled is False
    monkeypatch.setenv("MANUAL_CLIP_EDITS_ENABLED", "OFF")
    assert Config().manual_clip_edits_enabled is False
    monkeypatch.setenv("MANUAL_CLIP_EDITS_ENABLED", "true")
    assert Config().manual_clip_edits_enabled is True


@pytest.mark.parametrize("method,path,body", EDIT_ROUTES)
async def test_disabled_routes_answer_403(http, monkeypatch, method, path, body):
    monkeypatch.setenv("MANUAL_CLIP_EDITS_ENABLED", "false")
    response = await _call(http, method, path, body)
    assert response.status_code == 403
    assert response.json()["detail"] == DISABLED_DETAIL
    assert RecordingTaskService.calls == []


@pytest.mark.parametrize("method,path,body", EDIT_ROUTES)
async def test_enabled_routes_are_not_blocked(http, monkeypatch, method, path, body):
    monkeypatch.delenv("MANUAL_CLIP_EDITS_ENABLED", raising=False)
    response = await _call(http, method, path, body)
    detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else None
    assert detail != DISABLED_DETAIL


async def test_settings_applied_to_existing_clips_answer_403(http, monkeypatch):
    """Applying settings to existing clips re-renders every clip inside the API."""
    monkeypatch.setenv("MANUAL_CLIP_EDITS_ENABLED", "false")
    response = await http.post(
        "/tasks/t/settings", json={"font_size": 40, "apply_to_existing": True}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == DISABLED_DETAIL
    assert RecordingTaskService.calls == []


async def test_settings_without_apply_to_existing_are_not_blocked(http, monkeypatch):
    monkeypatch.setenv("MANUAL_CLIP_EDITS_ENABLED", "false")
    response = await http.post(
        "/tasks/t/settings", json={"font_size": 40, "apply_to_existing": False}
    )
    detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else None
    assert detail != DISABLED_DETAIL


async def test_other_routes_unaffected(http, monkeypatch):
    monkeypatch.setenv("MANUAL_CLIP_EDITS_ENABLED", "false")
    response = await http.patch("/tasks/t", json={"title": "new title"})
    detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else None
    assert detail != DISABLED_DETAIL
