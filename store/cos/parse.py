"""Translate COS response dictionaries into Cloudrave storage types."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from store.types import StorageObject, StorageObjectPage
from store.key import logical_key


def one_or_many(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, dict):
        return [value]
    return list(value)


def header(response: dict[str, Any], name: str) -> str | None:
    wanted = name.lower()
    for key, value in response.items():
        if key.lower() == wanted:
            return str(value)
    return None


def metadata(response: dict[str, Any]) -> dict[str, str]:
    prefix = "x-cos-meta-"
    return {
        str(key).lower()[len(prefix) :]: str(value)
        for key, value in response.items()
        if str(key).lower().startswith(prefix)
    }


def object_from_head(adapter: Any, policy: Any, mount: Any, object_key: str, response: dict[str, Any]) -> StorageObject:
    key = adapter._key(policy, mount, object_key)
    size_value = header(response, "content-length")
    return StorageObject(
        provider_type=adapter.provider_type,
        mount_id=str(mount.id),
        object_key=object_key,
        storage_path=Path(key),
        size=int(size_value) if size_value is not None else None,
        etag=header(response, "etag"),
        version_id=header(response, "x-cos-version-id"),
        mime_type=header(response, "content-type"),
        metadata=metadata(response),
    )


def page_from_response(adapter: Any, policy: Any, mount: Any, response: dict[str, Any]) -> StorageObjectPage:
    objects: list[StorageObject] = []
    last_physical: str | None = None
    for item in one_or_many(response.get("Contents")):
        physical = str(item.get("Key", ""))
        last_physical = physical or last_physical
        if not physical or physical.endswith("/"):
            continue
        relative = logical_key(policy, mount, physical)
        if not relative:  # Ignore a directory-marker object at the mount root.
            continue
        objects.append(
            StorageObject(
                provider_type=adapter.provider_type,
                mount_id=str(mount.id),
                object_key=relative,
                storage_path=Path(physical),
                size=int(item.get("Size", 0)),
                etag=str(item.get("ETag", "")),
                metadata={
                    "last_modified": str(item.get("LastModified", "")),
                    "storage_class": str(item.get("StorageClass", "")),
                },
            )
        )

    prefixes: list[str] = []
    for item in one_or_many(response.get("CommonPrefixes")):
        physical = str(item.get("Prefix", "")).rstrip("/")
        relative = logical_key(policy, mount, physical)
        if relative:
            prefixes.append(f"{relative}/")

    truncated = str(response.get("IsTruncated", "false")).lower() == "true"
    next_marker = str(response.get("NextMarker") or "") or None
    if truncated and not next_marker and last_physical:
        # COS may omit NextMarker without a delimiter; the last key is the
        # documented safe continuation point.
        next_marker = last_physical
    return StorageObjectPage(
        objects=objects,
        common_prefixes=prefixes,
        next_marker=next_marker,
        is_truncated=truncated,
    )
