"""TOTP two-factor authentication schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.schemas.auth import AuthSchema


class TwoFactorStatusResponse(AuthSchema):
    enabled: bool


class TwoFactorSetupPasswordRequest(AuthSchema):
    password: str = Field(min_length=8, max_length=128)


class TwoFactorSetupPasskeyRequest(AuthSchema):
    ceremony_id: str = Field(min_length=1, max_length=64)
    credential: dict[str, Any]


class TwoFactorSetupInitiateResponse(AuthSchema):
    setup_token: str
    secret: str
    qr_uri: str


class TwoFactorSetupConfirmRequest(AuthSchema):
    setup_token: str = Field(min_length=20, max_length=512)
    secret: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=6, max_length=8)


class TwoFactorSetupConfirmResponse(AuthSchema):
    enabled: bool
    backup_codes: list[str]


class TwoFactorDisableRequest(AuthSchema):
    code: str = Field(min_length=6, max_length=8)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    ceremony_id: str | None = Field(default=None, min_length=1, max_length=64)
    credential: dict[str, Any] | None = None
