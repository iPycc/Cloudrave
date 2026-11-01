"""Compatibility exports for the built-in drive registry."""

from __future__ import annotations

from drives.registry import DriveRegistry as StoreRegistry
from drives.registry import default_drive_registry as default_registry

__all__ = ["StoreRegistry", "default_registry"]
