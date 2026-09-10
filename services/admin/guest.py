"""Administrator-managed guest accounts and isolated workspaces."""

from __future__ import annotations

from datetime import UTC, datetime
import secrets
import string

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import UserRole, UserStatus
from models.tables import BucketMount, SystemMeta, User
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from repositories.sessions import SessionRepository
from repositories.uploads import UploadRepository
from repositories.users import UserRepository
from schemas.guest import (
    GuestCreateRequest,
    GuestCreateResponse,
    GuestCredentialResponse,
    GuestRead,
    GuestUpdateRequest,
)
from security.password import PasswordService
from services.user.guest_access import guest_expiry, guest_is_expired


GUEST_COUNTER_KEY = "guest.next_number"
GUEST_EMAIL_DOMAIN = "cloudrave.cn"
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "-_!@"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class GuestAdminService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        nodes: NodeRepository,
        uploads: UploadRepository,
        password_service: PasswordService,
    ) -> None:
        self.session = session
        self.users = users
        self.sessions = sessions
        self.mounts = mounts
        self.policies = policies
        self.nodes = nodes
        self.uploads = uploads
        self.password_service = password_service

    async def list_guests(self) -> list[GuestRead]:
        guests = await self.users.list_by_role(UserRole.GUEST)
        return [await self._read(guest, await self._workspace(guest)) for guest in guests]

    async def create_guest(self, actor: User, payload: GuestCreateRequest) -> GuestCreateResponse:
        source = await self.mounts.get_by_id(payload.source_mount_id)
        if source is None or source.owner_id != actor.id or (source.extra or {}).get("guest_workspace"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source mount not found")
        if not source.is_enabled or source.read_only:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Source mount is not writable")
        policy = await self.policies.get_by_id(source.policy_id)
        if policy is None or policy.status != "active":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Source storage policy is inactive")
        expires_at = self._validate_expiry(payload.expires_at)

        number = await self._next_guest_number()
        username = f"guest{number:02d}"
        email = f"{username}@{GUEST_EMAIL_DOMAIN}"
        password = self._generate_password()
        guest = await self.users.create_guest(
            email=email,
            username=username,
            password_hash=self.password_service.hash_password(password),
            timezone=actor.timezone or "UTC",
        )
        prefix = "/".join(part for part in (source.root_path.strip("/"), "guest", guest.uid) if part)
        workspace = await self.mounts.create_guest_workspace(
            owner_id=guest.id,
            policy_id=source.policy_id,
            source_mount_id=source.id,
            uid=guest.uid,
            root_path=prefix,
            quota_bytes=payload.quota_bytes,
            expires_at=expires_at.isoformat() if expires_at else None,
        )
        await self.nodes.ensure_mount_root(owner_id=guest.id, mount_id=workspace.id, name=workspace.name)
        return GuestCreateResponse(
            guest=await self._read(guest, workspace),
            credentials=GuestCredentialResponse(email=email, password=password),
        )

    async def update_guest(self, guest_id: int, payload: GuestUpdateRequest) -> GuestRead:
        guest = await self._guest(guest_id)
        workspace = await self._workspace(guest)
        if payload.enabled is not None:
            await self.users.set_status(guest, UserStatus.ACTIVE if payload.enabled else UserStatus.DISABLED)
            workspace.is_enabled = payload.enabled
            if not payload.enabled:
                await self.sessions.revoke_all_for_user(guest.id)
        if payload.quota_bytes is not None:
            used = await self.nodes.mount_file_bytes(workspace.id)
            if payload.quota_bytes < used:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Quota cannot be lower than used bytes")
            workspace.quota_bytes = payload.quota_bytes
        if payload.expires_at is not None or payload.clear_expiry:
            expires_at = None if payload.clear_expiry else self._validate_expiry(payload.expires_at)
            extra = dict(workspace.extra or {})
            extra["guest_expires_at"] = expires_at.isoformat() if expires_at else None
            workspace.extra = extra
            if expires_at is not None and expires_at <= datetime.now(UTC):
                await self.sessions.revoke_all_for_user(guest.id)
        await self.session.flush()
        return await self._read(guest, workspace)

    async def reset_password(self, guest_id: int) -> GuestCredentialResponse:
        guest = await self._guest(guest_id)
        password = self._generate_password()
        await self.users.update_password(guest, self.password_service.hash_password(password))
        await self.sessions.revoke_all_for_user(guest.id)
        return GuestCredentialResponse(email=guest.email, password=password)

    async def _read(self, guest: User, workspace: BucketMount) -> GuestRead:
        used = await self.nodes.mount_file_bytes(workspace.id)
        reserved = await self.uploads.reserved_bytes_for_mount(workspace.id)
        expiry = guest_expiry(workspace)
        return GuestRead(
            id=guest.id,
            uid=guest.uid,
            email=guest.email,
            username=guest.username,
            enabled=guest.status == UserStatus.ACTIVE and workspace.is_enabled,
            expired=guest_is_expired(workspace),
            expires_at=expiry,
            source_mount_id=int((workspace.extra or {}).get("guest_source_mount_id", 0)),
            workspace_mount_id=workspace.id,
            workspace_prefix=workspace.root_path,
            quota_bytes=int(workspace.quota_bytes or 0),
            used_bytes=used,
            reserved_bytes=reserved,
            created_at=guest.created_at,
        )

    async def _guest(self, guest_id: int) -> User:
        guest = await self.users.get_by_id(guest_id)
        if guest is None or guest.role != UserRole.GUEST:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Guest account not found")
        return guest

    async def _workspace(self, guest: User) -> BucketMount:
        mounts = await self.mounts.list_for_owner(guest.id)
        workspace = next((mount for mount in mounts if (mount.extra or {}).get("guest_workspace")), None)
        if workspace is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Guest workspace is missing")
        return workspace

    async def _next_guest_number(self) -> int:
        marker = await self.session.scalar(
            select(SystemMeta).where(SystemMeta.key == GUEST_COUNTER_KEY).with_for_update()
        )
        if marker is None:
            marker = SystemMeta(key=GUEST_COUNTER_KEY, value="0")
            self.session.add(marker)
            await self.session.flush()
        number = int(marker.value or 0)
        while True:
            number += 1
            email = f"guest{number:02d}@{GUEST_EMAIL_DOMAIN}"
            if await self.users.get_by_email(email) is None:
                marker.value = str(number)
                await self.session.flush()
                return number

    @staticmethod
    def _validate_expiry(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        expiry = _as_utc(value)
        if expiry <= datetime.now(UTC):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Expiry must be in the future")
        return expiry

    @staticmethod
    def _generate_password(length: int = 18) -> str:
        return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
