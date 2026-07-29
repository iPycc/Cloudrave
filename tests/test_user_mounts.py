from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import models  # noqa: F401
from api.v1.mount import require_mount_manager, router
from db.base import Base
from models.enums import NodeStatus, NodeType, StoragePolicyType, UserRole, UserStatus
from models.tables import Node, User
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from repositories.uploads import UploadRepository
from schemas.policy import StoragePolicyCreate, StoragePolicyUpdate
from schemas.user_mount import UserBucketMountCreate, UserMountCreate, UserMountUpdate
from services.user.mount import OWNER_KEY, USER_MANAGED_KEY, UserMountService


class _Adapter:
    def __init__(self) -> None:
        self.validated = 0
        self.cors = 0

    async def avalidate_mount(self, _policy, _mount):
        self.validated += 1
        return {"status": "ok"}

    async def aconfigure_cors(self, _policy, _mount):
        self.cors += 1
        return {"status": "configured"}


class _StorageManager:
    def __init__(self) -> None:
        self.adapter = _Adapter()

    def get_adapter(self, _policy):
        return self.adapter


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/mounts",
            "headers": [(b"origin", b"http://localhost:9130")],
            "client": ("127.0.0.1", 9130),
            "server": ("test", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def _user(*, user_id: int, uid: str, role: UserRole = UserRole.USER) -> User:
    return User(
        id=user_id,
        uid=uid,
        email=f"{uid.lower()}@example.test",
        username=uid.lower(),
        password_hash="test",
        role=role,
        status=UserStatus.ACTIVE,
    )


def _payload(name: str = "Personal COS") -> UserMountCreate:
    return UserMountCreate(
        policy=StoragePolicyCreate(
            name=name,
            provider=StoragePolicyType.TENCENT_COS,
            bucket_name="bucket-123456",
            region="ap-guangzhou",
            secret_id="abcdefgh12345678",
            secret_key="secret-key-12345678",
        ),
        mount=UserBucketMountCreate(
            name=name,
            mount_slug="personal-cos",
            provider_label="Tencent COS",
        ),
    )


def test_user_mount_routes_use_public_mount_prefix() -> None:
    paths = {(route.path, method) for route in router.routes for method in route.methods or set()}
    assert ("/mounts", "POST") in paths
    assert ("/mounts/{mount_id}", "PATCH") in paths
    assert ("/mounts/{mount_id}", "DELETE") in paths
    assert ("/mounts/{mount_id}/cors/auto-config", "POST") in paths
    assert all(not path.startswith("/admin/") for path, _method in paths)


def test_guests_cannot_manage_mounts() -> None:
    require_mount_manager(_user(user_id=1, uid="USER0001"))
    require_mount_manager(_user(user_id=2, uid="ADMIN001", role=UserRole.ADMIN))
    with pytest.raises(HTTPException) as exc_info:
        require_mount_manager(_user(user_id=3, uid="GUEST001", role=UserRole.GUEST))
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_user_mount_crud_is_owner_scoped_and_policy_is_atomic() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        actor = _user(user_id=1, uid="USER0001")
        other = _user(user_id=2, uid="USER0002")
        session.add_all([actor, other])
        await session.flush()

        storage_manager = _StorageManager()
        service = UserMountService(
            session=session,
            mounts=BucketMountRepository(session),
            policies=StoragePolicyRepository(session),
            nodes=NodeRepository(session),
            uploads=UploadRepository(session),
            audits=AuditLogRepository(session),
            storage_manager=storage_manager,
        )

        created = await service.create_mount(_request(), actor, _payload())
        mount_id = created.mount.id
        policy_id = created.policy.id

        assert created.mount.owner_id == actor.id
        assert created.mount.policy_id == policy_id
        assert created.policy.secret_id != "abcdefgh12345678"
        assert storage_manager.adapter.validated == 1

        policy = await StoragePolicyRepository(session).get_by_id(policy_id)
        assert policy is not None
        assert policy.extra[OWNER_KEY] == actor.id
        assert policy.extra[USER_MANAGED_KEY] is True

        with pytest.raises(HTTPException) as exc_info:
            await service.get_mount(other, mount_id)
        assert exc_info.value.status_code == 404

        updated = await service.update_mount(
            _request(),
            actor,
            mount_id,
            UserMountUpdate(
                policy=StoragePolicyUpdate(description="updated"),
                mount={"name": "Renamed COS"},
            ),
        )
        assert updated.mount.name == "Renamed COS"
        assert updated.policy.description == "updated"

        cors = await service.configure_cors(_request(), actor, mount_id)
        assert cors["status"] == "configured"
        assert storage_manager.adapter.cors == 1

        root = await NodeRepository(session).get_mount_root(mount_id)
        assert root is not None
        session.add(
            Node(
                owner_id=actor.id,
                mount_id=mount_id,
                parent_id=root.id,
                type=NodeType.FILE,
                status=NodeStatus.ACTIVE,
                name="keep.txt",
                blob_path="keep.txt",
                size=1,
            )
        )
        await session.flush()
        preview = await service.delete_preview(actor, mount_id)
        assert preview.has_contents is True
        assert preview.file_count == 1
        deleted = await service.delete_mount(_request(), actor, mount_id)
        assert deleted.policy_deleted is True
        assert deleted.deleted_objects == 0
        assert await BucketMountRepository(session).get_by_id(mount_id) is None
        assert await StoragePolicyRepository(session).get_by_id(policy_id) is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_local_mount_cannot_escape_user_upload_directory() -> None:
    actor = _user(user_id=7, uid="USER0007")
    service = UserMountService(
        session=SimpleNamespace(),
        mounts=SimpleNamespace(),
        policies=SimpleNamespace(),
        nodes=SimpleNamespace(),
        uploads=SimpleNamespace(),
        audits=SimpleNamespace(),
        storage_manager=SimpleNamespace(),
    )
    payload = UserBucketMountCreate(
        name="Local",
        mount_slug="local",
        extra={"storage_root": "/upload/another-user/"},
    )
    with pytest.raises(HTTPException) as exc_info:
        service._mount_create(actor, payload, policy_id=0, provider=StoragePolicyType.LOCAL_FS)
    assert exc_info.value.status_code == 403
