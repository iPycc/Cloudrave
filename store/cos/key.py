"""COS object-key and mount-prefix rules."""

from __future__ import annotations

from typing import Any

from store.types import normalize_object_key


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
    """Map a mount-relative key to COS without ambiguous prefix guessing."""
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
        raise ValueError("COS returned an object outside the mounted prefix")
    return normalized[len(expected) :]
