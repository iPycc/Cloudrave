"""Snapshots used to preserve and verify OSS objects during protected copies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from store.errors import storage_error_code


@dataclass(frozen=True, slots=True)
class CopySnapshot:
    size: int
    etag: str
    modified: Any
    crc64: str | None
    content_md5: str | None
    content_type: str | None
    cache_control: str | None
    content_disposition: str | None
    content_encoding: str | None
    expires: str | None
    storage_class: str
    encryption: str | None
    data_encryption: str | None
    encryption_key: str | None
    metadata: tuple[tuple[str, str], ...]
    acl: str
    tags: tuple[tuple[str, str], ...]

    def tagging_header(self) -> str | None:
        return urlencode(self.tags, quote_via=quote, safe="") if self.tags else None

    def encryption_args(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "server_side_encryption": self.encryption,
                "server_side_data_encryption": self.data_encryption,
                "server_side_encryption_key_id": self.encryption_key,
            }.items()
            if value
        }

    def preserved_attributes(self) -> tuple[Any, ...]:
        return (
            self.size, self.content_type, self.cache_control, self.content_disposition,
            self.content_encoding, self.expires, self.storage_class, self.encryption,
            self.data_encryption, self.encryption_key, self.metadata, self.acl, self.tags,
        )


def read_copy_snapshot(client: Any, oss: Any, bucket: str, key: str) -> CopySnapshot:
    """Read the identity and attributes needed to verify a copy; never change the source."""

    try:
        client.get_symlink(oss.GetSymlinkRequest(bucket=bucket, key=key))
    except Exception as exc:
        if storage_error_code(exc) != "NotSymlink":
            raise
    else:
        raise ValueError("OSS symbolic links require a separate migration")
    head = client.head_object(oss.HeadObjectRequest(bucket=bucket, key=key))
    size = getattr(head, "content_length", None)
    etag = str(getattr(head, "etag", "") or "").strip('"')
    modified = getattr(head, "last_modified", None)
    storage_class = getattr(head, "storage_class", None)
    if size is None or not etag or modified is None or not storage_class:
        raise ValueError("OSS source identity or storage class is unavailable for a protected copy")
    if getattr(head, "object_type", None) not in (None, "Normal", "Multipart"):
        raise ValueError("OSS appendable or unsupported objects cannot be safely copied")
    if any(getattr(head, name, None) for name in (
        "object_worm_retain_until_date", "object_worm_mode", "object_worm_legal_hold",
    )):
        raise ValueError("OSS objects with retention or legal hold require a separate migration")
    headers = {str(name).lower(): value for name, value in (getattr(head, "headers", None) or {}).items()}
    if headers.get("x-oss-persistent-headers"):
        raise ValueError("OSS persistent custom headers cannot be safely copied")
    acl = getattr(client.get_object_acl(oss.GetObjectAclRequest(bucket=bucket, key=key)), "acl", None)
    if acl not in {"default", "private", "public-read", "public-read-write"}:
        raise ValueError("OSS object ACL is unavailable for a protected copy")
    tagging = client.get_object_tagging(oss.GetObjectTaggingRequest(bucket=bucket, key=key))
    tag_set = getattr(tagging, "tag_set", None)
    raw_tags = getattr(tag_set, "tags", None) or []
    if any(not isinstance(getattr(tag, "key", None), str) or not tag.key for tag in raw_tags):
        raise ValueError("OSS object tags are incomplete")
    tags = tuple(sorted((tag.key, str(tag.value or "")) for tag in raw_tags))
    if len({name for name, _ in tags}) != len(tags):
        raise ValueError("OSS object tags are ambiguous")
    metadata = tuple(sorted((str(name), str(value)) for name, value in (getattr(head, "metadata", None) or {}).items()))
    return CopySnapshot(
        size=int(size), etag=etag, modified=modified,
        crc64=getattr(head, "hash_crc64", None), content_md5=getattr(head, "content_md5", None),
        content_type=getattr(head, "content_type", None), cache_control=getattr(head, "cache_control", None),
        content_disposition=getattr(head, "content_disposition", None),
        content_encoding=getattr(head, "content_encoding", None), expires=getattr(head, "expires", None),
        storage_class=str(storage_class), encryption=getattr(head, "server_side_encryption", None),
        data_encryption=getattr(head, "server_side_data_encryption", None),
        encryption_key=getattr(head, "server_side_encryption_key_id", None),
        metadata=metadata, acl=str(acl), tags=tags,
    )
