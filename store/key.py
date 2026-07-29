"""Immutable physical keys for Cloudrave-managed objects."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePath
from uuid import uuid4

from store.types import join_object_key


MANAGED_PREFIX = "blob"
LEGACY_MANAGED_PREFIX = ".cloudrave"


def managed_key(
    owner_id: int,
    filename: str,
    *,
    now: datetime | None = None,
    style: str = "readable",
) -> str:
    _ = owner_id, now or datetime.now(UTC)
    suffix = PurePath(filename).suffix.lower()
    if len(suffix) > 16 or not re.fullmatch(r"\.[a-z0-9]+", suffix):
        suffix = ""
    blob_id = uuid4().hex
    if style == "opaque":
        object_name = f"{blob_id}{suffix}"
    else:
        safe_name = PurePath(filename).name.replace("/", "_").replace("\\", "_")
        object_name = f"{blob_id}-{safe_name}"
    return join_object_key(MANAGED_PREFIX, object_name)


def is_managed_key(value: str) -> bool:
    normalized = value.replace("\\", "/").lstrip("/")
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in (MANAGED_PREFIX, LEGACY_MANAGED_PREFIX)
    )
