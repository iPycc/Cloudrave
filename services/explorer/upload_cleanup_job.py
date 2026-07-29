"""Periodic failed and inactive upload cleanup loop."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging

from db.base import session_scope
from repositories.mounts import BucketMountRepository
from repositories.policies import StoragePolicyRepository
from repositories.uploads import UploadRepository
from services.explorer.upload_cleanup import UploadCleanup
from store.manager import StorageManager


class UploadCleanupWorker:
    def __init__(self, session_factory, storage_manager: StorageManager, *, interval: int = 60) -> None:
        self.session_factory = session_factory
        self.storage_manager = storage_manager
        self.interval = interval
        self.log = logging.getLogger("cloudrave.store.upload-cleanup")
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="upload-cleanup")

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        self.task = None

    async def _loop(self) -> None:
        while True:
            try:
                async with session_scope(self.session_factory) as session:
                    cleaned = await UploadCleanup(
                        uploads=UploadRepository(session),
                        mounts=BucketMountRepository(session),
                        policies=StoragePolicyRepository(session),
                        storage_manager=self.storage_manager,
                    ).run_expired(limit=100)
                    if cleaned:
                        self.log.info("Cleaned %s failed or inactive upload session(s).", cleaned)
            except Exception:
                self.log.exception("Automatic upload cleanup failed")
            await asyncio.sleep(self.interval)
