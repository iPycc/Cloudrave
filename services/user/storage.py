"""User-scoped storage provisioning."""

from __future__ import annotations

from models.enums import StoragePolicyType, UserRole
from models.tables import User
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from schemas.policy import StoragePolicyCreate
from store.manager import StorageManager


class UserStorageProvisionService:
    def __init__(
        self,
        *,
        policies: StoragePolicyRepository,
        mounts: BucketMountRepository,
        nodes: NodeRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.policies = policies
        self.mounts = mounts
        self.nodes = nodes
        self.storage_manager = storage_manager

    async def ensure_default_local_storage(self, user: User) -> None:
        # Guest storage is provisioned only by an administrator and must stay
        # inside the selected source mount's isolated prefix.
        if user.role == UserRole.GUEST:
            return
        storage_root = f"/upload/{user.uid}/"
        policy_name = f"local-{user.uid}"
        default_extra = {
            "system_managed": True,
            "provider_label": "Local Storage",
            "storage_type": "local",
            "storage_root": storage_root,
        }

        policy = await self.policies.get_by_name(policy_name)
        if policy is None:
            policy = await self.policies.create(
                StoragePolicyCreate(
                    name=policy_name,
                    provider=StoragePolicyType.LOCAL_FS,
                    bucket_name=policy_name,
                    base_prefix="",
                    description=f"System managed local storage for {user.uid}",
                    multipart_threshold_mb=25,
                    part_size_mb=25,
                    presign_ttl_seconds=900,
                    is_default=user.role != UserRole.ADMIN,
                    extra=default_extra,
                )
            )
        else:
            policy.provider = StoragePolicyType.LOCAL_FS
            policy.bucket_name = policy.bucket_name or policy_name
            policy.description = policy.description or f"System managed local storage for {user.uid}"
            policy.multipart_threshold_mb = policy.multipart_threshold_mb or 25
            policy.part_size_mb = policy.part_size_mb or 25
            policy.presign_ttl_seconds = policy.presign_ttl_seconds or 900
            policy.extra = {**(policy.extra or {}), **default_extra}
            if user.role != UserRole.ADMIN:
                policy.is_default = True
            await self.policies.session.flush()

        mount = await self.mounts.get_by_owner_and_slug(user.id, user.uid)
        if mount is None:
            mount = await self.mounts.create_managed_local_mount(
                owner_id=user.id,
                policy_id=policy.id,
                name="Local Storage",
                mount_slug=user.uid,
                provider_label="Local Storage",
                root_path="",
                extra=default_extra,
            )
        else:
            mount.policy_id = policy.id
            mount.name = mount.name or "Local Storage"
            mount.provider_label = "Local Storage"
            mount.root_path = mount.root_path or ""
            mount.is_enabled = True
            mount.extra = {**(mount.extra or {}), **default_extra}
            await self.mounts.session.flush()

        await self.nodes.ensure_mount_root(owner_id=user.id, mount_id=mount.id, name=mount.name)
        self.storage_manager.get_adapter(policy).prepare_mount_root(mount)
