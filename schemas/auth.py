"""Authentication schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AuthSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterRequest(AuthSchema):
    email: EmailStr
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(AuthSchema):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class RefreshRequest(AuthSchema):
    refresh_token: str | None = Field(default=None, min_length=20, max_length=512)


class LogoutRequest(AuthSchema):
    scope: Literal["current", "all"] = "current"
    refresh_token: str | None = Field(default=None, min_length=20, max_length=512)


class UserResponse(AuthSchema):
    id: int
    uid: str
    email: str
    username: str
    role: str
    status: str
    timezone: str
    avatar: str | None = None
    created_at: datetime
    password_updated_at: datetime | None = None
    two_factor_enabled: bool = False


class TokenPairResponse(AuthSchema):
    access_token: str
    refresh_token: str | None = None
    access_expires_at: datetime
    refresh_expires_at: datetime
    session_id: str


class TwoFactorChallengeResponse(AuthSchema):
    requires_2fa: Literal[True] = True
    two_factor_token: str
    message: str = "请输入两步验证码"


class TwoFactorVerifyRequest(AuthSchema):
    two_factor_token: str = Field(min_length=20, max_length=512)
    code: str = Field(min_length=6, max_length=8)
    method: Literal["password", "passkey"] = "password"


class LoginResponse(AuthSchema):
    user: UserResponse
    token: TokenPairResponse | None = None
    requires_2fa: bool = False
    two_factor_token: str | None = None
    message: str | None = None
