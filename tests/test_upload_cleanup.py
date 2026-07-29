from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from models.enums import UploadMode, UploadState
from services.explorer.upload_cleanup import UploadCleanup


class _Uploads:
    def __init__(self, record) -> None:
        self.record = record
        self.states: list[UploadState] = []

    async def list_cleanup_candidates(self, *, stale_before, limit: int):
        return [self.record]

    async def list_active_for_mount(self, mount_id: int):
        return [self.record]

    async def transition_state(self, upload_session_id: str, *, expected, target: UploadState) -> bool:
        assert self.record.state in expected
        self.record.state = target
        self.states.append(target)
        return True

    async def set_state(self, upload_session_id: str, state: UploadState):
        self.record.state = state
        self.states.append(state)
        return self.record


class _Lookup:
    def __init__(self, value) -> None:
        self.value = value

    async def get_by_id(self, value_id: int):
        return self.value


class _Adapter:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def prepare_mount_root(self, mount) -> Path:
        return Path("upload")

    def delete_object(self, policy, mount, object_key: str, *, hard_delete: bool = False) -> bool:
        assert hard_delete is True
        self.deleted.append(object_key)
        return True


class _StorageManager:
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def get_adapter(self, policy):
        return self.adapter


@pytest.mark.asyncio
async def test_failed_upload_is_cleaned_and_expired() -> None:
    record = SimpleNamespace(
        id="upload-1",
        mount_id=3,
        state=UploadState.FAILED,
        mode=UploadMode.SINGLE,
        path="blob/orphan.bin",
        upload_id=None,
        part_size=1,
        size=1,
    )
    mount = SimpleNamespace(id=3, policy_id=7)
    policy = SimpleNamespace(id=7, provider=SimpleNamespace(value="tencent_cos"))
    uploads = _Uploads(record)
    adapter = _Adapter()
    cleanup = UploadCleanup(
        uploads=uploads,
        mounts=_Lookup(mount),
        policies=_Lookup(policy),
        storage_manager=_StorageManager(adapter),
    )

    cleaned = await cleanup.run_expired()

    assert cleaned == 1
    assert adapter.deleted == ["blob/orphan.bin"]
    assert uploads.states == [UploadState.ABORTING, UploadState.EXPIRED]
