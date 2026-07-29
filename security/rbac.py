"""Simple RBAC helpers."""

from __future__ import annotations

from fastapi import HTTPException, status

from models.enums import UserRole
from models.tables import User


def require_admin(user: User) -> None:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
