"""Provider-neutral managed-object and mounted-prefix key rules."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePath, PurePosixPath
from string import Formatter
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
    directory: str = "",
    template: str | None = None,
) -> str:
    _ = owner_id, now or datetime.now(UTC)
    suffix = PurePath(filename).suffix.lower()
    if len(suffix) > 16 or not re.fullmatch(r"\.[a-z0-9]+", suffix):
        suffix = ""
    safe_name = PurePath(filename).name.replace("/", "_").replace("\\", "_")
    blob_id = uuid4().hex
    if template is None:
        object_name = f"{blob_id}{suffix}" if style == "opaque" else f"{blob_id}-{safe_name}"
        return join_object_key(MANAGED_PREFIX, object_name)
    return format_object_key(template, directory=directory, filename=safe_name, blob_id=blob_id)


def format_object_key(template: str, *, directory: str, filename: str, blob_id: str) -> str:
    """Render a mount-relative object key from validated path components."""

    if not template or "\\" in template or template.startswith("/") or template.endswith("/"):
        raise ValueError("Invalid object key template")
    directory_parts = directory.split("/") if directory else []
    if any(not part or part in {".", ".."} or "\\" in part or "\x00" in part for part in directory_parts):
        raise ValueError("Invalid directory path")
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise ValueError("Invalid file name")
    path = PurePosixPath(filename)
    values = {"filename": filename, "stem": path.stem, "ext": path.suffix, "blob_id": blob_id}
    parts: list[str] = []
    fields: set[str] = set()
    for segment in template.split("/"):
        if segment == "{dir}":
            if "dir" in fields:
                raise ValueError("Template may contain the directory variable only once")
            fields.add("dir")
            parts.extend(directory_parts)
            continue
        if not segment:
            raise ValueError("Empty template segment")
        fragments: list[str] = []
        try:
            for literal, field, specification, conversion in Formatter().parse(segment):
                fragments.append(literal)
                if field is not None:
                    if field not in values or specification or conversion:
                        raise ValueError("Unknown or invalid template variable")
                    fields.add(field)
                    fragments.append(values[field])
        except ValueError as exc:
            raise ValueError("Invalid object key template") from exc
        part = "".join(fragments)
        if not part or part in {".", ".."} or "/" in part or "\\" in part or "\x00" in part:
            raise ValueError("Invalid object key segment")
        parts.append(part)
    if "dir" not in fields or not fields.intersection({"filename", "stem", "blob_id"}):
        raise ValueError("Template must contain a directory and a file name variable")
    result = "/".join(parts)
    if not result or len(result.encode("utf-8")) > 1024:
        raise ValueError("Object key exceeds the storage limit")
    if result == LEGACY_MANAGED_PREFIX or result.startswith(f"{LEGACY_MANAGED_PREFIX}/"):
        raise ValueError("Object key uses a reserved prefix")
    return result


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
    # Keep the historical trimming behavior for configured mount prefixes.
    return normalize_object_key(value.strip())


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


def validate_physical_key(key: str, *, max_bytes: int, provider: str) -> str:
    """Validate the complete provider key, including the configured mount prefix."""

    if not key or "\x00" in key or len(key.encode("utf-8")) > max_bytes:
        raise ValueError(f"{provider} object key exceeds the provider limit or contains NUL")
    return key


def physical_list_prefix(policy: Any, mount: Any, prefix: str = "") -> str:
    mounted = mount_prefix(policy, mount)
    if not prefix:
        return f"{mounted}/" if mounted else ""
    result = physical_key(policy, mount, prefix)
    if prefix.replace("\\", "/").endswith("/"):
        return f"{result}/"
    return result


def logical_key(policy: Any, mount: Any, key: str) -> str:
    # Listing must retain the exact provider key. Invalid virtual path segments
    # are reported as import conflicts instead of being silently normalized.
    normalized = str(key)
    prefix = mount_prefix(policy, mount)
    if not prefix:
        return normalized
    if normalized == prefix:
        return ""
    expected = f"{prefix}/"
    if not normalized.startswith(expected):
        raise ValueError("Storage provider returned an object outside the mounted prefix")
    return normalized[len(expected) :]
