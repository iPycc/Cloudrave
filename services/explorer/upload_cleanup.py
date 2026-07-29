"""Automatic cleanup for failed and inactive upload sessions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from models.enums import UploadState
from repositories.mounts import BucketMountRepository
from repositories.policies import StoragePolicyRepository
from repositories.uploads import UploadRepository
from services.explorer.up_abort import cleanup_upload
from services.explorer.up_check import build_upload_state
from store.manager import StorageManager


UPLOAD_INACTIVITY_TIMEOUT = timedelta(minutes=15)


class UploadCleanup:
    """Remove provider remnants after an upload fails or becomes inactive."""

    def __init__(
        self,
        *,
        uploads: UploadRepository,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.uploads = uploads
        self.mounts = mounts
        self.policies = policies
        self.storage_manager = storage_manager
        self.log = logging.getLogger("cloudrave.store.upload-cleanup")

    async def run_expired(self, *, limit: int = 100) -> int:
        stale_before = datetime.now(UTC) - UPLOAD_INACTIVITY_TIMEOUT
        candidates = await self.uploads.list_cleanup_candidates(
            stale_before=stale_before,
            limit=limit,
        )
        cleaned = 0
        for record in candidates:
            try:
                if await self._cleanup_record(record):
                    cleaned += 1
            except Exception:
                await self.uploads.set_state(record.id, UploadState.FAILED)
                self.log.exception("Upload remnant cleanup failed for session %s", record.id)
        return cleaned

    async def cleanup_mount(self, mount_id: int) -> int:
        """Clean every non-terminal upload before purging a mount."""

        cleaned = 0
        for record in await self.uploads.list_active_for_mount(mount_id):
            if await self._cleanup_record(record):
                cleaned += 1
        return cleaned

    async def _cleanup_record(self, record) -> bool:
        if record.state != UploadState.ABORTING:
            claimed = await self.uploads.transition_state(
                record.id,
                expected=(
                    UploadState.CREATED,
                    UploadState.UPLOADING,
                    UploadState.COMPLETING,
                    UploadState.FAILED,
                ),
                target=UploadState.ABORTING,
            )
            if not claimed:
                return False

        mount = await self.mounts.get_by_id(record.mount_id)
        if mount is None:
            await self.uploads.set_state(record.id, UploadState.EXPIRED)
            return True
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            await self.uploads.set_state(record.id, UploadState.EXPIRED)
            return True

        adapter = self.storage_manager.get_adapter(policy)
        upload = build_upload_state(record, policy, mount, self.storage_manager)
        await cleanup_upload(adapter, policy, mount, upload, record.mode)
        await self.uploads.set_state(record.id, UploadState.EXPIRED)
        return True
