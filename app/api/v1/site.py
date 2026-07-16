"""Public site and admin website settings routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from app.middleware.auth_context import get_current_user
from app.models.tables import User
from app.repositories.site_settings import SiteSettingsRepository
from app.schemas.site import WebsiteSettings, WebsiteSettingsResponse
from app.security.rbac import require_admin
from app.services.site.settings import WebsiteSettingsService


router = APIRouter(prefix="/site", tags=["site"])


@router.get("/settings", response_model=WebsiteSettingsResponse)
async def get_website_settings(request: Request) -> WebsiteSettingsResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = WebsiteSettingsService(SiteSettingsRepository(session))
        return WebsiteSettingsResponse(settings=await service.get())


@router.put("/settings", response_model=WebsiteSettingsResponse)
async def update_website_settings(
    request: Request,
    payload: WebsiteSettings = Body(...),
    current_user: User = Depends(get_current_user),
) -> WebsiteSettingsResponse:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = WebsiteSettingsService(SiteSettingsRepository(session))
        return WebsiteSettingsResponse(settings=await service.update(payload))
