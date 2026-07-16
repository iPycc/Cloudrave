"""Storage adapter manager."""

from __future__ import annotations

from app.models.enums import StoragePolicyType
from app.models.tables import BucketMount, StoragePolicy
from app.storage.types import join_object_key
from app.storage_adapters.local_fs import LocalFsAdapter
from app.storage_adapters.tencent_cos import TencentCosAdapter
from config import CloudraveConfig


class StorageManager:
    def __init__(self, *, config: CloudraveConfig, local_root) -> None:
        self.adapters = {
            StoragePolicyType.LOCAL_FS.value: LocalFsAdapter(local_root),
            StoragePolicyType.TENCENT_COS.value: TencentCosAdapter(config.tencent_cos),
        }

    def get_adapter(self, policy: StoragePolicy):
        adapter = self.adapters.get(policy.provider.value)
        if adapter is None:
            raise KeyError(f"storage adapter {policy.provider.value!r} is not registered")
        return adapter

    def build_object_key(self, mount: BucketMount, file_name: str, parent_blob_path: str | None = None) -> str:
        mount_root = mount.root_path.strip("/")
        if parent_blob_path:
            return join_object_key(mount_root, parent_blob_path, file_name)
        return join_object_key(mount_root, file_name)
