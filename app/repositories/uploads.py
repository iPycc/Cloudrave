"""Upload persistence operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UploadMode, UploadState
from app.models.tables import UploadPartRecord, UploadSessionRecord


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
    ) -> UploadSessionRecord:
        record = UploadSessionRecord(
            owner_id=owner_id,
            mount_id=mount_id,
            node_id=node_id,
            path=path,
            mode=mode,
            state=UploadState.CREATED,
            upload_id=upload_id,
            file_name=file_name,
            content_type=content_type,
            size=size,
            part_size=part_size,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_session(self, upload_session_id: str) -> UploadSessionRecord | None:
        return await self.session.get(UploadSessionRecord, upload_session_id)

    async def list_parts(self, upload_session_id: str) -> list[UploadPartRecord]:
        result = await self.session.scalars(
            select(UploadPartRecord)
            .where(UploadPartRecord.upload_session_id == upload_session_id)
            .order_by(UploadPartRecord.part_number.asc())
        )
        return list(result)

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

    async def bind_node(self, upload_session_id: str, node_id: int) -> UploadSessionRecord | None:
        record = await self.get_session(upload_session_id)
        if record is None:
            return None
        record.node_id = node_id
        await self.session.flush()
        return record
