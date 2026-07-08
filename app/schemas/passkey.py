"""Passkey and WebAuthn schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.auth import AuthSchema, LoginResponse


class PasskeyRegistrationOptionsRequest(AuthSchema):
    name: str | None = Field(default=None, min_length=1, max_length=128)


class PasskeyRegistrationVerifyRequest(AuthSchema):
    ceremony_id: str = Field(min_length=1, max_length=64)
    credential: dict[str, Any]
    name: str | None = Field(default=None, min_length=1, max_length=128)


class PasskeyRenameRequest(AuthSchema):
    name: str = Field(min_length=1, max_length=128)


class PasskeyAuthenticationOptionsRequest(AuthSchema):
    email: str | None = Field(default=None, max_length=255)


class PasskeyAuthenticationVerifyRequest(AuthSchema):
    ceremony_id: str = Field(min_length=1, max_length=64)
    credential: dict[str, Any]


class PasskeyRead(AuthSchema):
    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None = None
    transports: list[str] = Field(default_factory=list)


class PasskeyOptionsResponse(AuthSchema):
    ceremony_id: str
    options: dict[str, Any]


class PasskeyVerificationResponse(AuthSchema):
    verified: bool = True
    passkey: PasskeyRead


class PasskeyListResponse(AuthSchema):
    passkeys_enabled: bool
    passkeys: list[PasskeyRead]


class PasskeyAuthenticationResponse(AuthSchema):
    verified: Literal[True] = True
    login: LoginResponse
