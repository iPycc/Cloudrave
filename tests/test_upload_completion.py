import asyncio
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
