"""User profile and security schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field, model_validator

from schemas.auth import AuthSchema, UserResponse


class UserProfileUpdateRequest(AuthSchema):
    email: EmailStr
    username: str = Field(min_length=2, max_length=64)


class PasswordChangeRequest(AuthSchema):
    current_password: str | None = Field(default=None, min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)
    ceremony_id: str | None = Field(default=None, min_length=1, max_length=128)
    credential: dict | None = Field(default=None)


class UserPreferencesUpdateRequest(AuthSchema):
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    thumbnails_enabled: bool | None = None

    @model_validator(mode="after")
    def require_preference(self):
        if self.timezone is None and self.thumbnails_enabled is None:
            raise ValueError("At least one preference is required")
        return self


class UserPreferencesResponse(AuthSchema):
    timezone: str
    thumbnails_enabled: bool


class LoginActivityEntry(AuthSchema):
    id: int
    method: str
    result: str
    device: str
    ip: str | None = None
    time: datetime
    identifier: str


class UserSecurityResponse(AuthSchema):
    password_updated_at: datetime | None = None
    login_activity: list[LoginActivityEntry]


class UserProfileEnvelope(AuthSchema):
    user: UserResponse
