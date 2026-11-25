import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from core.cfg import load_config
from db.base import Base
from models.enums import NodeType, UploadMode
from models.tables import User
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from repositories.uploads import UploadRepository
from schemas.mount import BucketMountCreate
from schemas.policy import StoragePolicyCreate
from services.explorer.preview import PreviewService, preview_version
from services.explorer.shared_io import SharedIo
from services.explorer.source import FileSourceService
from services.explorer.write_policy import StorageWritePolicy
from store.manager import StorageManager


@pytest.fixture
async def workspace(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = StorageManager(config=load_config(tmp_path), local_root=tmp_path / "files")
    async with factory() as session:
        actor = User(uid="USER1", username="user", email="user@example.test", password_hash="test")
        session.add(actor)
        await session.flush()
        policy = await StoragePolicyRepository(session).create(StoragePolicyCreate(
            name="local", provider="local_fs", bucket_name="local"))
        mount = await BucketMountRepository(session).create(actor.id, BucketMountCreate(
            policy_id=policy.id, name="Local", mount_slug="local", quota_bytes=4))
        nodes = NodeRepository(session)
        root = await nodes.ensure_mount_root(owner_id=actor.id, mount_id=mount.id, name="Local")
        node = await nodes.create_file(owner_id=actor.id, mount_id=mount.id, parent_id=root.id,
            name="proof.txt", blob_path="proof.txt", size=3)
        storage.get_adapter(policy).put_object(policy, mount, "proof.txt", b"old")
        await session.commit()
        yield SimpleNamespace(session=session, factory=factory, actor=actor, mount=mount,
            policy=policy, node=node, nodes=nodes, storage=storage)
    await engine.dispose()


def source(workspace):
    return FileSourceService(mounts=BucketMountRepository(workspace.session), nodes=workspace.nodes,
        policies=StoragePolicyRepository(workspace.session), objects=ObjectRepository(workspace.session),
        storage_manager=workspace.storage)


@pytest.mark.asyncio
async def test_text_growth_checks_quota_before_touching_object(workspace):
    w = workspace
    service = PreviewService(resolver=source(w), nodes=w.nodes, objects=ObjectRepository(w.session),
        audits=AuditLogRepository(w.session), annotations=None, storage_manager=w.storage,
        token_service=None, cache_root=None, public_base_url="", office_ttl_seconds=10, text_max_bytes=100)
    with pytest.raises(HTTPException) as denied:
        await service.save_text(w.actor, w.node.id, content="large", if_match=preview_version(w.node))
    assert denied.value.status_code == 413
    with w.storage.get_adapter(w.policy).open_object(w.policy, w.mount, "proof.txt") as stream:
        assert stream.read() == b"old"
    node_id = w.node.id
    await w.session.rollback()
    await w.session.refresh(w.actor)
    node = await w.nodes.get_by_id(node_id)
    result = await service.save_text(w.actor, node.id, content="four", if_match=preview_version(node))
    assert result.size == 4


@pytest.mark.asyncio
async def test_shared_save_checks_budget_before_consuming_share_or_writing(workspace):
    w = workspace
    recorder = AsyncMock()
    shared = SimpleNamespace(resolve_node=AsyncMock(return_value=(object(),
        SimpleNamespace(id=999, type=NodeType.FILE, size=10))),
        source=SimpleNamespace(record_download=recorder))
    service = SharedIo(shared=shared, source=source(w), archive=None)
    with pytest.raises(HTTPException) as denied:
        await service.save(w.actor, 1, 999, w.mount.id, None)
    assert denied.value.status_code == 413
    recorder.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_reservations_and_inactive_policies_apply_to_other_writes(workspace):
    w = workspace
    await UploadRepository(w.session).create_session(owner_id=w.actor.id, mount_id=w.mount.id,
        node_id=None, path="pending", mode=UploadMode.SINGLE, upload_id=None, file_name="pending",
        content_type=None, size=1, part_size=1)
    with pytest.raises(HTTPException) as denied:
        await StorageWritePolicy(w.session).check(w.actor, w.mount.id, 1)
    assert denied.value.status_code == 413
    w.policy.status = "inactive"
    await w.session.flush()
    with pytest.raises(HTTPException) as denied:
        await StorageWritePolicy(w.session).check(w.actor, w.mount.id)
    assert denied.value.status_code == 409


@pytest.mark.asyncio
async def test_sqlite_concurrent_writers_cannot_both_spend_remaining_quota(workspace):
    w = workspace
    actor_id, mount_id = w.actor.id, w.mount.id
    await w.session.close()

    async def write(name):
        async with w.factory.begin() as session:
            actor = await session.get(User, actor_id)
            try:
                await StorageWritePolicy(session).check(actor, mount_id, 1)
            except HTTPException as denied:
                return denied.status_code
            await asyncio.sleep(0.02)
            await NodeRepository(session).create_file(owner_id=actor_id, mount_id=mount_id,
                parent_id=None, name=name, blob_path=name, size=1)
            return 200

    assert sorted(await asyncio.gather(write("a.txt"), write("b.txt"))) == [200, 413]
