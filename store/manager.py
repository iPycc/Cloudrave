"""Storage adapter manager."""

from __future__ import annotations

from pathlib import Path

from core.cfg import CloudraveConfig
from models.tables import BucketMount, StoragePolicy
from store.reg import StoreRegistry, default_registry
from store.types import join_object_key, normalize_object_key


class StorageManager:
    def __init__(
        self,
        *,
        config: CloudraveConfig,
        local_root: str | Path,
        registry: StoreRegistry | None = None,
    ) -> None:
        self.registry = registry or default_registry()
        self.adapters = self.registry.create_all(
            config=config,
            local_root=Path(local_root),
        )

    def get_adapter(self, policy: StoragePolicy):
        adapter = self.adapters.get(policy.provider.value)
        if adapter is None:
            raise KeyError(f"storage adapter {policy.provider.value!r} is not registered")
        return adapter

    def local_path(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        object_key: str,
    ) -> Path | None:
        return self.get_adapter(policy).local_path(mount, object_key)

    def build_object_key(self, mount: BucketMount, file_name: str, parent_blob_path: str | None = None) -> str:
        if parent_blob_path:
            return join_object_key(parent_blob_path, file_name)
        return normalize_object_key(file_name)
