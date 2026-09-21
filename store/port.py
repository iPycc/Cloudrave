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

    def validate_completed_object(self, policy, mount, stored_object, checksum: str | None = None) -> None:
        """Validate provider-specific completion guarantees."""

        _ = (policy, mount)
        if checksum and self.capabilities.upload_transport == "presigned":
            metadata = getattr(stored_object, "metadata", {}) or {}
            if metadata.get("sha256") != checksum:
                raise RuntimeError("Uploaded object checksum metadata does not match the session")

