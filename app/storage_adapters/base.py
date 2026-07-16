"""Base storage adapter."""
from __future__ import annotations

from abc import ABC
from pathlib import Path

from app.storage.types import StorageCapability


class StorageAdapterBase(ABC):
    provider_type: str
    capabilities: StorageCapability

    def prepare_mount_root(self, mount):  # type: ignore[override]
        raise NotImplementedError

    def _ensure_directory(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

