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
    avatar: str | None = None
    created_at: datetime
    password_updated_at: datetime | None = None


class TokenPairResponse(AuthSchema):
    access_token: str
    refresh_token: str | None = None
    access_expires_at: datetime
    refresh_expires_at: datetime
    session_id: str


class LoginResponse(AuthSchema):
    user: UserResponse
    token: TokenPairResponse
