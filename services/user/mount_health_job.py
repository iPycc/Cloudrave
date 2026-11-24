"""Refresh mount connection status in the background."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from models.tables import User
from services.user.mount import UserMountService


class MountHealthWorker:
    def __init__(self, dep, *, interval: int = 300) -> None:
        self.dep = dep
        self.interval = interval
        self.task: asyncio.Task[None] | None = None
        self.log = logging.getLogger("cloudrave.mount-health")

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="mount-health")

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
                async with self.dep.session_scope() as session:
                    mounts = await self.dep.get_mount_repository(session).list_all()
                    targets = [(mount.id, mount.owner_id) for mount in mounts]
                for mount_id, owner_id in targets:
                    try:
                        async with self.dep.session_scope() as session:
                            await UserMountService(
                                session=session,
                                mounts=self.dep.get_mount_repository(session),
                                policies=self.dep.get_policy_repository(session),
                                nodes=self.dep.get_node_repository(session),
                                uploads=self.dep.get_upload_repository(session),
                                audits=self.dep.get_audit_repository(session),
                                storage_manager=self.dep.storage_manager,
                            ).check_health(User(id=owner_id), mount_id, self.dep.config.app.public_base_url)
                    except Exception:
                        self.log.exception("Mount health check failed for mount %s", mount_id)
            except Exception:
                self.log.exception("Periodic mount health check failed")
            await asyncio.sleep(self.interval)
