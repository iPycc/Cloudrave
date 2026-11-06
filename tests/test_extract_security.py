import asyncio
import io
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

import py7zr
import pytest
from fastapi import HTTPException

from test_write_security import workspace
from models.enums import UserRole
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from repositories.users import UserRepository
from services.explorer import extract


async def prepare(w, *, suffix="zip", quota_extra=100, role=UserRole.USER, read_only=False):
    buffer = io.BytesIO()
    if suffix == "zip":
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("inside.txt", b"hello")
    else:
        with py7zr.SevenZipFile(buffer, "w") as archive:
            archive.writestr(b"hello", "inside.txt")
    content = buffer.getvalue()
    w.node.name = f"proof.{suffix}"
    w.node.size = len(content)
    w.mount.quota_bytes = len(content) + quota_extra
    w.mount.extra = {**w.mount.extra, "read_only": read_only}
    w.actor.role = role
    w.storage.get_adapter(w.policy).put_object(w.policy, w.mount, w.node.blob_path, content)
    await w.session.commit()

    @asynccontextmanager
    async def scope():
        async with w.factory.begin() as session:
            yield session

    return SimpleNamespace(session_scope=scope, storage_manager=w.storage,
        get_user_repository=UserRepository, get_mount_repository=BucketMountRepository,
        get_policy_repository=StoragePolicyRepository, get_node_repository=NodeRepository,
        get_object_repository=ObjectRepository, get_audit_repository=AuditLogRepository)


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [{"role": UserRole.GUEST}, {"read_only": True}])
async def test_extraction_rejects_guest_and_read_only_mount_before_queueing(workspace, settings):
    w = workspace
    dep = await prepare(w, **settings)
    before = set(extract._tasks)
    with pytest.raises(HTTPException) as denied:
        await extract.start_extract(dep, w.actor.id, w.node.id, None)
    assert denied.value.status_code == 403
    assert set(extract._tasks) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["zip", "7z"])
@pytest.mark.parametrize("quota_extra,expected", [(4, "failed"), (5, "completed")])
async def test_extraction_enforces_quota_for_zip_and_7z(workspace, suffix, quota_extra, expected):
    w = workspace
    dep = await prepare(w, suffix=suffix, quota_extra=quota_extra)
    started = await extract.start_extract(dep, w.actor.id, w.node.id, None)
    await asyncio.wait_for(asyncio.gather(*list(extract._running)), timeout=15)
    task = extract._tasks[started.id]
    assert task.status == expected
    async with w.factory() as session:
        used = await NodeRepository(session).mount_file_bytes(w.mount.id)
        assert used <= w.mount.quota_bytes
        if expected == "failed":
            assert task.result_node_id is None
        else:
            children = await NodeRepository(session).list_children(w.mount.id, task.result_node_id)
            assert [node.name for node in children] == ["inside.txt"]
            with w.storage.get_adapter(w.policy).open_object(w.policy, w.mount, children[0].blob_path) as stream:
                assert stream.read() == b"hello"


@pytest.mark.asyncio
async def test_background_extraction_rechecks_mount_state_before_reading(workspace):
    w = workspace
    dep = await prepare(w)
    task = SimpleNamespace(owner_id=w.actor.id)
    w.mount.is_enabled = False
    await w.session.commit()
    with pytest.raises(HTTPException) as denied:
        await extract._load_extract_source(dep, task, w.node.id, None)
    assert denied.value.status_code == 409
