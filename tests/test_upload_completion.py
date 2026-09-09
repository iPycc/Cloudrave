import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.base import Base
from models.enums import UploadMode, UploadState
from repositories.uploads import UploadRepository
from schemas.upload import UploadSessionCompleteRequest
from services.explorer.upload import CreateUploadSessionService
from store.types import MultipartPart


@pytest.mark.asyncio
async def test_only_one_concurrent_request_can_claim_completion(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'uploads.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            record = await UploadRepository(session).create_session(
                owner_id=1, mount_id=1, node_id=None, path="test", mode=UploadMode.SINGLE,
                upload_id=None, file_name="test.txt", content_type=None, size=1, part_size=1)
            record_id = record.id
        async def claim():
            async with sessions.begin() as session:
                return await UploadRepository(session).transition_state(
                    record_id, expected=(UploadState.CREATED, UploadState.UPLOADING, UploadState.FAILED),
                    target=UploadState.COMPLETING)
        assert sorted(await asyncio.gather(claim(), claim())) == [False, True]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_upload_retry_returns_existing_result_without_storage_io():
    service = object.__new__(CreateUploadSessionService)
    service._require_upload_access = AsyncMock(return_value=SimpleNamespace(state=UploadState.COMPLETED))
    service._to_read = AsyncMock(return_value="existing-result")
    assert await service.complete(None, None, "id", UploadSessionCompleteRequest()) == "existing-result"


@pytest.mark.asyncio
async def test_in_progress_completion_returns_conflict_without_repeating_storage_io():
    service = object.__new__(CreateUploadSessionService)
    service._require_upload_access = AsyncMock(return_value=SimpleNamespace(id="id", state=UploadState.COMPLETING))
    service.uploads = SimpleNamespace(transition_state=AsyncMock(return_value=False))
    service.session = SimpleNamespace(refresh=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await service.complete(None, None, "id", UploadSessionCompleteRequest())
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_sync_parts_replaces_stale_database_records(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'parts.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            uploads = UploadRepository(session)
            record = await uploads.create_session(
                owner_id=1,
                mount_id=1,
                node_id=None,
                path="test",
                mode=UploadMode.MULTIPART,
                upload_id="provider-id",
                file_name="test.bin",
                content_type=None,
                size=3,
                part_size=1,
            )
            await uploads.upsert_part(upload_session_id=record.id, part_number=1, etag="stale", size=1)
            await uploads.upsert_part(upload_session_id=record.id, part_number=2, etag="old", size=1)
            synced = await uploads.sync_parts(record.id, [(2, "fresh", 1), (3, "last", 1)])

            assert [(part.part_number, part.etag) for part in synced] == [(2, "fresh"), (3, "last")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_upload_reconciles_provider_parts_before_returning_session():
    class _Adapter:
        def list_parts(self, policy, mount, upload):
            return [
                MultipartPart(part_number=1, etag="etag-1", size=5),
                MultipartPart(part_number=2, etag="etag-2", size=3),
            ]

    record = SimpleNamespace(
        id="session-id",
        mount_id=9,
        mode=UploadMode.MULTIPART,
        state=UploadState.UPLOADING,
    )
    service = object.__new__(CreateUploadSessionService)
    service._require_upload_access = AsyncMock(return_value=record)
    service._require_mount_access = AsyncMock(return_value=("mount", "policy"))
    service._build_upload_state = lambda *_args: "upload"
    service._expected_part_size = lambda _record, part_number: 5 if part_number == 1 else 3
    service.storage_manager = SimpleNamespace(get_adapter=lambda _policy: _Adapter())
    service.uploads = SimpleNamespace(sync_parts=AsyncMock(), touch=AsyncMock())
    service._to_read = AsyncMock(return_value="session")

    result = await service.get(SimpleNamespace(), record.id)

    assert result == "session"
    service.uploads.sync_parts.assert_awaited_once_with(
        record.id,
        [(1, "etag-1", 5), (2, "etag-2", 3)],
    )
    service.uploads.touch.assert_awaited_once_with(record.id)


@pytest.mark.asyncio
async def test_recent_failed_upload_remains_available_for_resume(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            uploads = UploadRepository(session)
            record = await uploads.create_session(
                owner_id=1,
                mount_id=1,
                node_id=None,
                path="resume.bin",
                mode=UploadMode.MULTIPART,
                upload_id="provider-id",
                file_name="resume.bin",
                content_type=None,
                size=1,
                part_size=1,
                state=UploadState.FAILED,
            )
            record.updated_at = datetime.now(UTC)
            await session.flush()

            candidates = await uploads.list_cleanup_candidates(
                stale_before=datetime.now(UTC) - timedelta(days=7),
            )

            assert candidates == []
    finally:
        await engine.dispose()
