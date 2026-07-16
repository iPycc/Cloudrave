"""User response builders."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from app.models.tables import User
from app.schemas.auth import UserResponse


def build_user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        uid=user.uid,
        email=user.email,
        username=user.username,
        role=user.role.value,
        status=user.status.value,
        avatar=build_avatar_url(user),
        created_at=_trim_to_seconds(user.created_at),
        password_updated_at=_trim_to_seconds(user.password_updated_at),
    )


def build_avatar_url(user: User) -> str | None:
    if not user.avatar_path:
        return None

    avatar_file = Path(user.avatar_path).name
    return f"/api/v1/user/avatar/{user.uid}/{avatar_file}"


def _trim_to_seconds(value):
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)
