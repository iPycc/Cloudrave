"""Read-only guest workspace availability helpers used by authentication paths."""

from __future__ import annotations

from datetime import UTC, datetime

from models.tables import BucketMount, User
from repositories.mounts import BucketMountRepository


def guest_expiry(mount: BucketMount) -> datetime | None:
    value = (mount.extra or {}).get("guest_expires_at")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def guest_is_expired(mount: BucketMount, *, now: datetime | None = None) -> bool:
    expiry = guest_expiry(mount)
    return expiry is not None and expiry <= (now or datetime.now(UTC))


async def guest_workspace(user: User, mounts: BucketMountRepository) -> BucketMount | None:
    owned_mounts = await mounts.list_for_owner(user.id)
    return next((mount for mount in owned_mounts if (mount.extra or {}).get("guest_workspace")), None)


async def guest_is_available(user: User, mounts: BucketMountRepository) -> bool:
    workspace = await guest_workspace(user, mounts)
    return workspace is not None and workspace.is_enabled and not guest_is_expired(workspace)
