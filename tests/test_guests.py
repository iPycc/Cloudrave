from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models  # noqa: F401
from db.base import Base
from models.enums import StoragePolicyType, UserRole, UserStatus
from models.tables import User
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from repositories.sessions import SessionRepository
from repositories.uploads import UploadRepository
from repositories.users import UserRepository
from schemas.guest import GuestCreateRequest
from schemas.mount import BucketMountCreate
from schemas.policy import StoragePolicyCreate
from security.capabilities import Capability, capabilities_for_role, require_capability
from security.password import PasswordService
from services.admin.guest import GuestAdminService
from services.explorer.access import MountAccessPolicy


@pytest.mark.asyncio
async def test_admin_creates_isolated_guest_with_quota_and_credentials() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        admin = User(
            uid="ADMIN",
            email="admin@example.test",
            username="admin",
            password_hash="test",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(admin)
        await session.flush()

        policies = StoragePolicyRepository(session)
        policy = await policies.create(
            StoragePolicyCreate(
                name="guest-source",
                provider=StoragePolicyType.LOCAL_FS,
                bucket_name="guest-source",
            )
        )
        mounts = BucketMountRepository(session)
        source = await mounts.create(
            admin.id,
            BucketMountCreate(
                policy_id=policy.id,
                name="Guest Source",
                mount_slug="guest-source",
                root_path="tenants",
            ),
        )
        service = GuestAdminService(
            session=session,
            users=UserRepository(session),
            sessions=SessionRepository(session),
            mounts=mounts,
            policies=policies,
            nodes=NodeRepository(session),
            uploads=UploadRepository(session),
            password_service=PasswordService(),
        )

        created = await service.create_guest(
            admin,
            GuestCreateRequest(source_mount_id=source.id, quota_bytes=50 * 1024 * 1024),
        )

        assert created.guest.email == "guest01@cloudrave.cn"
        assert created.guest.workspace_prefix == f"tenants/guest/{created.guest.uid}"
        assert created.guest.quota_bytes == 50 * 1024 * 1024
        assert len(created.credentials.password) == 18
        guest = await UserRepository(session).get_by_id(created.guest.id)
        assert guest is not None and guest.role == UserRole.GUEST
        assert PasswordService().verify_password(created.credentials.password, guest.password_hash)

    await engine.dispose()


@pytest.mark.asyncio
async def test_mount_access_policy_has_consistent_read_write_semantics() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        owner = User(uid="OWNER", email="owner@example.test", username="owner", password_hash="test")
        outsider = User(uid="OTHER", email="other@example.test", username="other", password_hash="test")
        session.add_all([owner, outsider])
        await session.flush()
        policies = StoragePolicyRepository(session)
        policy = await policies.create(StoragePolicyCreate(name="local", provider=StoragePolicyType.LOCAL_FS, bucket_name="local"))
        mounts = BucketMountRepository(session)
        mount = await mounts.create(owner.id, BucketMountCreate(policy_id=policy.id, name="Local", mount_slug="local"))
        access = MountAccessPolicy(mounts=mounts, policies=policies)

        assert (await access.read(owner, mount.id))[0].id == mount.id
        with pytest.raises(HTTPException) as hidden:
            await access.read(outsider, mount.id)
        assert hidden.value.status_code == 404

        mount.is_enabled = False
        with pytest.raises(HTTPException) as disabled:
            await access.read(owner, mount.id)
        assert disabled.value.status_code == 409
        assert (await access.cleanup(owner, mount.id))[0].id == mount.id

    await engine.dispose()


def test_guest_capabilities_are_enforced_server_side() -> None:
    guest = User(
        uid="GUEST",
        email="guest@example.test",
        username="guest",
        password_hash="test",
        role=UserRole.GUEST,
    )
    assert capabilities_for_role(UserRole.GUEST) == {
        Capability.FILE_READ,
        Capability.FILE_WRITE,
        Capability.PASSWORD_CHANGE,
    }
    with pytest.raises(HTTPException) as denied:
        require_capability(guest, Capability.SHARE_MANAGE)
    assert denied.value.status_code == 403
