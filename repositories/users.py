"""User persistence operations."""

from __future__ import annotations

import secrets
import string

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import UserRole, UserStatus
from models.tables import SystemMeta, User, utcnow


UID_ALPHABET = string.ascii_uppercase + string.digits
UID_LENGTH = 5


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(func.lower(User.email) == email.lower())
        return await self.session.scalar(stmt)

    async def get_by_username(self, username: str) -> User | None:
        stmt = select(User).where(func.lower(User.username) == username.lower())
        return await self.session.scalar(stmt)

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_uid(self, uid: str) -> User | None:
        stmt = select(User).where(User.uid == uid)
        return await self.session.scalar(stmt)

    async def list_by_role(self, role: UserRole) -> list[User]:
        result = await self.session.scalars(select(User).where(User.role == role).order_by(User.id.asc()))
        return list(result)

    async def create_guest(
        self,
        *,
        email: str,
        username: str,
        password_hash: str,
        timezone: str = "UTC",
    ) -> User:
        user = User(
            uid=await self._generate_unique_uid(),
            email=email.lower(),
            username=username,
            password_hash=password_hash,
            avatar_path=None,
            role=UserRole.GUEST,
            status=UserStatus.ACTIVE,
            timezone=timezone,
            password_updated_at=utcnow(),
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def set_status(self, user: User, status: UserStatus) -> User:
        user.status = status
        await self.session.flush()
        return user

    async def create_first_admin_aware(
        self,
        email: str,
        username: str,
        password_hash: str,
        timezone: str = "UTC",
    ) -> User:
        admin_state = await self.session.scalar(
            select(SystemMeta)
            .where(SystemMeta.key == "auth.first_admin_user_id")
            .with_for_update()
        )
        if admin_state is None:
            admin_state = SystemMeta(key="auth.first_admin_user_id", value="")
            self.session.add(admin_state)
            await self.session.flush()

        role = UserRole.ADMIN if not admin_state.value else UserRole.USER
        user = User(
            uid=await self._generate_unique_uid(),
            email=email.lower(),
            username=username,
            password_hash=password_hash,
            avatar_path=None,
            role=role,
            status=UserStatus.ACTIVE,
            timezone=timezone,
            password_updated_at=utcnow(),
        )
        self.session.add(user)
        await self.session.flush()

        if role == UserRole.ADMIN:
            admin_state.value = str(user.id)
            await self.session.flush()

        return user

    async def update_profile(self, user: User, *, email: str, username: str) -> User:
        user.email = email.lower()
        user.username = username
        await self.session.flush()
        return user

    async def update_avatar_path(self, user: User, avatar_path: str | None) -> User:
        user.avatar_path = avatar_path
        await self.session.flush()
        return user

    async def update_password(self, user: User, password_hash: str) -> User:
        user.password_hash = password_hash
        user.password_updated_at = utcnow()
        await self.session.flush()
        return user

    async def update_timezone(self, user: User, timezone: str) -> User:
        user.timezone = timezone
        await self.session.flush()
        return user

    async def update_preferences(
        self,
        user: User,
        *,
        timezone: str | None = None,
        thumbnails_enabled: bool | None = None,
    ) -> User:
        if timezone is not None:
            user.timezone = timezone
        if thumbnails_enabled is not None:
            user.thumbnails_enabled = thumbnails_enabled
        await self.session.flush()
        return user

    async def update_totp(self, user: User, *, secret: str | None, enabled: bool, backup_codes: list[str] | None = None) -> User:
        user.totp_secret = secret
        user.totp_enabled = enabled
        if backup_codes is not None:
            user.totp_backup_codes = backup_codes
        await self.session.flush()
        return user

    async def _generate_unique_uid(self) -> str:
        for _ in range(20):
            candidate = "".join(secrets.choice(UID_ALPHABET) for _ in range(UID_LENGTH))
            if await self.get_by_uid(candidate) is None:
                return candidate

        raise RuntimeError("Failed to generate unique UID for user")
