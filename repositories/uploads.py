"""Upload persistence operations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import UploadMode, UploadState
from models.tables import UploadPartRecord, UploadSessionRecord


class UploadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_session(
        self,
        *,
        owner_id: int,
        mount_id: int,
        node_id: int | None,
        path: str,
        mode: UploadMode,
        upload_id: str | None,
        file_name: str,
        content_type: str | None,
        size: int,
        part_size: int,
        checksum: str | None = None,
        provider_checksum: str | None = None,
        state: UploadState = UploadState.CREATED,
    ) -> UploadSessionRecord:
        record = UploadSessionRecord(
            owner_id=owner_id,
            mount_id=mount_id,
            node_id=node_id,
            path=path,
            mode=mode,
            state=state,
            upload_id=upload_id,
            file_name=file_name,
            content_type=content_type,
            checksum=checksum,
            provider_checksum=provider_checksum,
            size=size,
            part_size=part_size,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_session(self, upload_session_id: str) -> UploadSessionRecord | None:
        return await self.session.get(UploadSessionRecord, upload_session_id)

    async def list_active_for_mount(self, mount_id: int) -> list[UploadSessionRecord]:
        terminal = (UploadState.COMPLETED, UploadState.ABORTED, UploadState.EXPIRED)
        result = await self.session.scalars(
            select(UploadSessionRecord)
            .where(
                UploadSessionRecord.mount_id == mount_id,
                UploadSessionRecord.state.not_in(terminal),
            )
            .order_by(UploadSessionRecord.created_at.asc())
        )
        return list(result)

    async def reserved_bytes_for_mount(self, mount_id: int) -> int:
        active = (UploadState.CREATED, UploadState.UPLOADING, UploadState.COMPLETING)
        total = await self.session.scalar(
            select(func.coalesce(func.sum(UploadSessionRecord.size), 0)).where(
                UploadSessionRecord.mount_id == mount_id,
                UploadSessionRecord.state.in_(active),
            )
        )
        return int(total or 0)

    async def list_cleanup_candidates(
        self,
        *,
        stale_before: datetime,
        limit: int = 100,
    ) -> list[UploadSessionRecord]:
        stale_states = (
            UploadState.CREATED,
            UploadState.UPLOADING,
            UploadState.COMPLETING,
        )
        result = await self.session.scalars(
            select(UploadSessionRecord)
            .where(
                or_(
                    UploadSessionRecord.state == UploadState.ABORTING,
                    and_(
                        UploadSessionRecord.state.in_((*stale_states, UploadState.FAILED)),
                        UploadSessionRecord.updated_at <= stale_before,
                    ),
                )
            )
            .order_by(UploadSessionRecord.updated_at.asc(), UploadSessionRecord.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result)

    async def list_parts(self, upload_session_id: str) -> list[UploadPartRecord]:
        result = await self.session.scalars(
            select(UploadPartRecord)
            .where(UploadPartRecord.upload_session_id == upload_session_id)
            .order_by(UploadPartRecord.part_number.asc())
        )
        return list(result)

    async def sync_parts(
        self,
        upload_session_id: str,
        parts: list[tuple[int, str | None, int]],
    ) -> list[UploadPartRecord]:
        """Replace recorded parts with the storage provider's authoritative list."""

        part_numbers = {part_number for part_number, _etag, _size in parts}
        stale = delete(UploadPartRecord).where(
            UploadPartRecord.upload_session_id == upload_session_id
        )
        if part_numbers:
            stale = stale.where(UploadPartRecord.part_number.not_in(part_numbers))
        await self.session.execute(stale)
        for part_number, etag, size in parts:
            await self.upsert_part(
                upload_session_id=upload_session_id,
                part_number=part_number,
                etag=etag,
                size=size,
            )
        await self.session.flush()
        return await self.list_parts(upload_session_id)

    async def upsert_part(self, *, upload_session_id: str, part_number: int, etag: str | None, size: int) -> UploadPartRecord:
        record = await self.session.scalar(
            select(UploadPartRecord).where(
                UploadPartRecord.upload_session_id == upload_session_id,
                UploadPartRecord.part_number == part_number,
            )
        )
        if record is None:
            record = UploadPartRecord(
                upload_session_id=upload_session_id,
                part_number=part_number,
                etag=etag,
                size=size,
            )
            self.session.add(record)
        else:
            record.etag = etag
            record.size = size
        await self.session.flush()
        return record

    async def set_state(self, upload_session_id: str, state: UploadState) -> UploadSessionRecord | None:
        record = await self.get_session(upload_session_id)
        if record is None:
            return None
        record.state = state
        await self.session.flush()
        return record

    async def touch(self, upload_session_id: str) -> UploadSessionRecord | None:
        record = await self.get_session(upload_session_id)
        if record is None:
            return None
        record.updated_at = datetime.now(UTC)
        await self.session.flush()
        return record

    async def touch_active(self, upload_session_id: str) -> bool:
        result = await self.session.execute(
            update(UploadSessionRecord)
            .where(
                UploadSessionRecord.id == upload_session_id,
                UploadSessionRecord.state.in_(
                    (UploadState.CREATED, UploadState.UPLOADING, UploadState.COMPLETING, UploadState.FAILED)
                ),
            )
            .values(updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        return result.rowcount == 1

    async def transition_state(
        self,
        upload_session_id: str,
        *,
        expected: tuple[UploadState, ...],
        target: UploadState,
    ) -> bool:
        """Claim a state transition atomically across application instances."""

        result = await self.session.execute(
            update(UploadSessionRecord)
            .where(
                UploadSessionRecord.id == upload_session_id,
                UploadSessionRecord.state.in_(expected),
            )
            .values(state=target, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        return result.rowcount == 1

    async def bind_node(self, upload_session_id: str, node_id: int) -> UploadSessionRecord | None:
        record = await self.get_session(upload_session_id)
        if record is None:
            return None
        record.node_id = node_id
        await self.session.flush()
        return record
