"""Periodic retry loop for the object-GC outbox."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging

from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.repositories.policies import StoragePolicyRepository
from app.services.explorer.gc import ObjectGc
from app.storage.manager import StorageManager
from database import session_scope


class GcWorker:
    def __init__(self, session_factory, storage_manager: StorageManager, *, interval: int = 60) -> None:
        self.session_factory = session_factory
        self.storage_manager = storage_manager
        self.interval = interval
        self.log = logging.getLogger("cloudrave.storage.gc")
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="object-gc")

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        self.task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                async with session_scope(self.session_factory) as session:
                    await ObjectGc(
                        mounts=BucketMountRepository(session),
                        nodes=NodeRepository(session),
                        policies=StoragePolicyRepository(session),
                        objects=ObjectRepository(session),
                        storage_manager=self.storage_manager,
                    ).run_pending(limit=100)
            except Exception:
                self.log.exception("Object cleanup retry failed")
