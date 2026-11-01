"""Provider-neutral managed-object and mounted-prefix key rules."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any
from uuid import uuid4

from store.types import join_object_key, normalize_object_key


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


def clean_prefix(value: str | None) -> str:
    """Normalize an optional prefix without rejecting an empty root."""

    if not value or not value.strip("/\\ "):
        return ""
    return normalize_object_key(value)


def mount_prefix(policy: Any, mount: Any) -> str:
    parts = [
        clean_prefix(getattr(policy, "base_prefix", "")),
        clean_prefix(getattr(mount, "root_path", "")),
    ]
    return "/".join(part for part in parts if part)


def physical_key(policy: Any, mount: Any, object_key: str) -> str:
    """Map a mount-relative key without ambiguous prefix guessing."""

    logical = normalize_object_key(object_key)
    base = clean_prefix(getattr(policy, "base_prefix", ""))
    root = clean_prefix(getattr(mount, "root_path", ""))
    prefix = "/".join(part for part in (base, root) if part)
    if not prefix:
        return logical

    # Legacy prefixed keys must be enabled for the entire mount explicitly.
    # Mixing inferred legacy and relative keys is non-injective (for example,
    # both a.txt and root/a.txt could otherwise resolve to the same object).
    if bool((getattr(mount, "extra", None) or {}).get("legacy_prefixed_keys")):
        if logical == prefix or logical.startswith(f"{prefix}/"):
            return logical
        if root and (logical == root or logical.startswith(f"{root}/")):
            return "/".join(part for part in (base, logical) if part)
    return f"{prefix}/{logical}"


def physical_list_prefix(policy: Any, mount: Any, prefix: str = "") -> str:
    mounted = mount_prefix(policy, mount)
    if not prefix:
        return f"{mounted}/" if mounted else ""
    result = physical_key(policy, mount, prefix)
    if prefix.replace("\\", "/").endswith("/"):
        return f"{result}/"
    return result


def logical_key(policy: Any, mount: Any, key: str) -> str:
    normalized = normalize_object_key(key)
    prefix = mount_prefix(policy, mount)
    if not prefix:
        return normalized
    if normalized == prefix:
        return ""
    expected = f"{prefix}/"
    if not normalized.startswith(expected):
        raise ValueError("Storage provider returned an object outside the mounted prefix")
    return normalized[len(expected) :]
