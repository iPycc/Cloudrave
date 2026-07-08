"""Passkey persistence operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import PasskeyCeremony, PasskeyCredential


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class PasskeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_user(self, user_id: int) -> list[PasskeyCredential]:
        stmt = (
            select(PasskeyCredential)
            .where(PasskeyCredential.user_id == user_id)
            .order_by(PasskeyCredential.created_at.desc(), PasskeyCredential.id.desc())
        )
        result = await self.session.scalars(stmt)
        return list(result)

    async def get_for_user(self, user_id: int, passkey_id: int) -> PasskeyCredential | None:
        stmt = select(PasskeyCredential).where(
            PasskeyCredential.user_id == user_id,
            PasskeyCredential.id == passkey_id,
        )
        return await self.session.scalar(stmt)

    async def get_by_credential_id(self, credential_id: str) -> PasskeyCredential | None:
        stmt = select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id)
        return await self.session.scalar(stmt)

    async def create(
        self,
        *,
        user_id: int,
        name: str,
        credential_id: str,
        public_key: str,
        sign_count: int,
        aaguid: str | None,
        transports: list[str] | None,
    ) -> PasskeyCredential:
        record = PasskeyCredential(
            user_id=user_id,
            name=name,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            aaguid=aaguid,
            transports=transports or [],
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def update_usage(
        self,
        record: PasskeyCredential,
        *,
        sign_count: int,
        last_used_at: datetime | None = None,
    ) -> PasskeyCredential:
        record.sign_count = sign_count
        record.last_used_at = last_used_at or datetime.now(UTC)
        await self.session.flush()
        return record

    async def delete(self, record: PasskeyCredential) -> None:
        await self.session.delete(record)
        await self.session.flush()

    async def rename(self, record: PasskeyCredential, name: str) -> PasskeyCredential:
        record.name = name
        await self.session.flush()
        return record

    async def create_ceremony(
        self,
        *,
        ceremony_type: str,
        challenge: str,
        user_id: int | None = None,
        email_hint: str | None = None,
        ttl_seconds: int = 300,
    ) -> PasskeyCeremony:
        record = PasskeyCeremony(
            user_id=user_id,
            ceremony_type=ceremony_type,
            challenge=challenge,
            email_hint=email_hint,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_active_ceremony(self, ceremony_id: str, ceremony_type: str) -> PasskeyCeremony | None:
        stmt = select(PasskeyCeremony).where(
            PasskeyCeremony.id == ceremony_id,
            PasskeyCeremony.ceremony_type == ceremony_type,
            PasskeyCeremony.used_at.is_(None),
        )
        record = await self.session.scalar(stmt)
        if record is None:
            return None
        if _ensure_utc(record.expires_at) <= datetime.now(UTC):
            return None
        return record

    async def mark_ceremony_used(self, record: PasskeyCeremony) -> PasskeyCeremony:
        record.used_at = datetime.now(UTC)
        await self.session.flush()
        return record

    async def delete_expired_ceremonies(self) -> None:
        await self.session.execute(
            delete(PasskeyCeremony).where(PasskeyCeremony.expires_at < datetime.now(UTC))
        )
        await self.session.flush()
