"""Role capabilities shared by authentication and authorization checks."""

from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException, status

from models.enums import UserRole
from models.tables import User


class Capability(StrEnum):
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    FILE_COPY = "file.copy"
    FILE_EDIT = "file.edit"
    SHARE_MANAGE = "share.manage"
    SHARED_MOUNT = "shared.mount"
    TASK_MANAGE = "task.manage"
    MOUNT_MANAGE = "mount.manage"
    PROFILE_MANAGE = "profile.manage"
    SECURITY_MANAGE = "security.manage"
    PASSWORD_CHANGE = "password.change"
    ADMIN = "admin"


_GUEST_CAPABILITIES = frozenset(
    {
        Capability.FILE_READ,
        Capability.FILE_WRITE,
        Capability.PASSWORD_CHANGE,
    }
)

_USER_CAPABILITIES = frozenset(Capability) - {Capability.ADMIN}


def capabilities_for_role(role: UserRole) -> frozenset[Capability]:
    if role == UserRole.GUEST:
        return _GUEST_CAPABILITIES
    if role == UserRole.ADMIN:
        return frozenset(Capability)
    return _USER_CAPABILITIES


def has_capability(user: User, capability: Capability) -> bool:
    return capability in capabilities_for_role(user.role)


def require_capability(user: User, capability: Capability) -> None:
    if not has_capability(user, capability):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Capability required: {capability.value}",
        )
