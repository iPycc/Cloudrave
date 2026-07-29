"""Base class shared by storage drivers."""
from __future__ import annotations

from abc import ABC
from pathlib import Path

from store.types import StorageCapability


class StorageAdapterBase(ABC):
    provider_type: str
    capabilities: StorageCapability

    def prepare_mount_root(self, mount):  # type: ignore[override]
        raise NotImplementedError

    def local_path(self, mount, object_key: str) -> Path | None:
        """Return a validated local path when the driver can expose one."""

        _ = (mount, object_key)
        return None

    def _ensure_directory(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

