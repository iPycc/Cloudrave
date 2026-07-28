"""Public site and admin website settings routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field

from web.mid.auth import get_current_user
from models.tables import User
from repositories.site_settings import SiteSettingsRepository
from schemas.site import WebsiteSettings, WebsiteSettingsResponse
from security.rbac import require_admin
from services.site.settings import WebsiteSettingsService, detect_site_url


router = APIRouter(prefix="/site", tags=["site"])


class DetectedSiteUrlResponse(BaseModel):
    site_url: str = Field(..., max_length=512)


class SiteUrlStatusResponse(BaseModel):
    saved_site_url: str = Field(..., max_length=512)
    detected_site_url: str = Field(..., max_length=512)
    matches: bool


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


@router.get("/detected-url", response_model=DetectedSiteUrlResponse)
async def get_detected_site_url(request: Request) -> DetectedSiteUrlResponse:
    return DetectedSiteUrlResponse(site_url=detect_site_url(request))


@router.get("/url-status", response_model=SiteUrlStatusResponse)
async def get_site_url_status(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> SiteUrlStatusResponse:
    require_admin(current_user)
    dep = request.app.state.dep
    detected = detect_site_url(request)
    async with dep.session_scope() as session:
        service = WebsiteSettingsService(SiteSettingsRepository(session))
        saved = (await service.get_site_url()).rstrip("/")
    return SiteUrlStatusResponse(
        saved_site_url=saved,
        detected_site_url=detected,
        matches=saved == detected.rstrip("/"),
    )
