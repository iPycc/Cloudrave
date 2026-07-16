"""Site and website settings schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WebsiteSettings(BaseModel):
    site_title: str = Field(default="", max_length=128)
    site_logo_url: str = Field(default="", max_length=512)
    site_description: str = Field(default="", max_length=1024)
    seo_title: str = Field(default="", max_length=128)
    seo_description: str = Field(default="", max_length=256)
    seo_keywords: str = Field(default="", max_length=256)
    site_theme: str = Field(default="", max_length=64)
    site_font: str = Field(default="", max_length=64)
    site_border_radius: str = Field(default="0.75rem", max_length=32)
    footer_enabled: bool = Field(default=True)
    footer_html: str = Field(default="", max_length=4096)
    filing_enabled: bool = Field(default=False)
    filing_text: str = Field(default="", max_length=512)


class WebsiteSettingsResponse(BaseModel):
    settings: WebsiteSettings
