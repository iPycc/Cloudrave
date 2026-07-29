"""Cross-request locks for physical object references."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.blob import BlobRef


class BlobUnavailable(RuntimeError):
    pass


class BlobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock(self, physical_id: str) -> BlobRef:
        blob = await self.session.scalar(
            select(BlobRef).where(BlobRef.physical_id == physical_id).with_for_update()
        )
        if blob is not None:
            return blob
        try:
            async with self.session.begin_nested():
                blob = BlobRef(physical_id=physical_id)
                self.session.add(blob)
                await self.session.flush()
        except IntegrityError:
            blob = await self.session.scalar(
                select(BlobRef).where(BlobRef.physical_id == physical_id).with_for_update()
            )
        if blob is None:
            raise RuntimeError("Unable to lock physical object identity")
        return blob

    async def ensure(self, physical_id: str, *, revive: bool = False) -> BlobRef:
        blob = await self.lock(physical_id)
        if blob.state != "active":
            if not revive:
                raise BlobUnavailable("Physical object is being deleted or is no longer available")
            blob.state = "active"
            await self.session.flush()
        return blob

    async def deleting(self, blob: BlobRef) -> None:
        blob.state = "deleting"
        await self.session.flush()

    async def active(self, blob: BlobRef) -> None:
        blob.state = "active"
        await self.session.flush()

    async def deleted(self, blob: BlobRef) -> None:
        blob.state = "deleted"
        await self.session.flush()
