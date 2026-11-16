"""
Webhook settings routes.

Authenticated with the frontend's signed session headers only (never an API
key), like the API key routes.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth_headers import get_authenticated_user_id
from ...config import get_config
from ...database import get_db
from ...webhooks.service import WebhookService
from ...webhooks.url_safety import WebhookUrlError

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookSettingsBody(BaseModel):
    url: str
    enabled: bool = True


def _get_user_id(request: Request) -> str:
    """Resolve the user from signed session headers (never from an API key)."""
    return get_authenticated_user_id(request, get_config())


@router.get("/")
async def get_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = _get_user_id(request)
    return await WebhookService(db).get_settings(user_id)


@router.put("/")
async def save_webhook(
    body: WebhookSettingsBody, request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = _get_user_id(request)
    try:
        return await WebhookService(db).save(
            user_id, url=body.url, enabled=body.enabled
        )
    except WebhookUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/rotate-secret")
async def rotate_webhook_secret(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = _get_user_id(request)
    secret = await WebhookService(db).rotate_secret(user_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Webhook is not configured")
    return {"secret": secret}


@router.delete("/")
async def delete_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = _get_user_id(request)
    return {"deleted": await WebhookService(db).delete(user_id)}


@router.post("/test")
async def test_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = _get_user_id(request)
    delivery_id = await WebhookService(db).send_test(user_id)
    if delivery_id is None:
        raise HTTPException(
            status_code=409, detail="Webhook is not configured or disabled"
        )
    return {"delivery_id": delivery_id}
