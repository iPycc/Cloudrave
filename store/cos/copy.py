"""COS object snapshots for guarded copies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from store.cos.parse import header, metadata, one_or_many
from store.errors import storage_error_code


def _acl_parts(acl: dict[str, Any]) -> tuple[str, tuple[tuple[str, str, str, str], ...]]:
    mode = acl.get("CannedACL")
    owner = acl.get("Owner") or {}
    entries = acl.get("AccessControlList") or {}
    if mode not in {"default", "private", "public-read"} or not owner.get("ID"):
        raise ValueError("COS object ACL is unavailable for a protected copy")
    grants = []
    for grant in one_or_many(entries.get("Grant")):
        grantee = grant.get("Grantee") or {}
        kind = grantee.get("Type")
        identity = grantee.get("ID") or ""
        uri = grantee.get("URI") or ""
        permission = grant.get("Permission")
        if (kind not in {"CanonicalUser", "Group"} or not permission
                or (kind == "CanonicalUser" and not identity) or (kind == "Group" and not uri)):
            raise ValueError("COS object ACL contains an unsupported grant")
        grants.append((str(kind), str(identity), str(uri), str(permission)))
    return str(mode), tuple(sorted(grants))


@dataclass(frozen=True, slots=True)
class CopySnapshot:
    size: int
    etag: str
    modified: str
    crc64: str | None
    content_md5: str | None
    attributes: tuple[tuple[str, str], ...]
    metadata: tuple[tuple[str, str], ...]
    acl_mode: str
    acl_owner: str
    acl_grants: tuple[tuple[str, str, str, str], ...]
    tags: tuple[tuple[str, str], ...]

    def tagging_header(self) -> str | None:
        return urlencode(self.tags, quote_via=quote, safe="") if self.tags else None

    def acl_args(self) -> dict[str, str]:
        owner_grant = ("CanonicalUser", self.acl_owner, "", "FULL_CONTROL")
        canned = {owner_grant}
        if self.acl_mode == "public-read":
            canned.add(("Group", "", "http://cam.qcloud.com/groups/global/AllUsers", "READ"))
        if self.acl_mode == "default" or set(self.acl_grants) == canned:
            return {"ACL": self.acl_mode}
        result = {}
        for kind, identity, _uri, permission in self.acl_grants:
            if kind != "CanonicalUser" or permission not in {"READ", "WRITE", "FULL_CONTROL"} or any(
                char in identity for char in '\r\n"'
            ):
                raise ValueError("COS object ACL cannot be installed atomically during a protected copy")
            name = {"READ": "GrantRead", "WRITE": "GrantWrite", "FULL_CONTROL": "GrantFullControl"}[permission]
            grant = f'id="{identity}"'
            result[name] = f"{result[name]},{grant}" if name in result else grant
        if not result:
            raise ValueError("COS object ACL is unavailable for a protected copy")
        return result

    def write_args(self) -> dict[str, str]:
        names = {
            "content-type": "ContentType", "cache-control": "CacheControl",
            "content-disposition": "ContentDisposition", "content-encoding": "ContentEncoding",
            "content-language": "ContentLanguage", "expires": "Expires", "x-cos-storage-class": "StorageClass",
            "x-cos-server-side-encryption": "ServerSideEncryption",
            "x-cos-server-side-encryption-cos-kms-key-id": "SSEKMSKeyId",
            "x-cos-server-side-encryption-context": "SSEKMSContext",
        }
        return {names[name]: value for name, value in self.attributes}

    def preserved_attributes(self) -> tuple[Any, ...]:
        return self.size, self.attributes, self.metadata, self.acl_mode, self.acl_owner, self.acl_grants, self.tags


def read_copy_snapshot(client: Any, bucket: str, key: str) -> CopySnapshot:
    try:
        client.get_symlink(Bucket=bucket, SymlinkName=key)
    except Exception as exc:
        if storage_error_code(exc) != "NotSymlink":
            raise
    else:
        raise ValueError("COS symbolic links require a separate migration")
    head = client.head_object(Bucket=bucket, Key=key)
    raw_size = header(head, "content-length")
    etag = header(head, "etag")
    modified = header(head, "last-modified")
    if raw_size is None or not etag or not modified:
        raise ValueError("COS source identity is unavailable for a protected copy")
    lowered = {str(name).lower(): value for name, value in head.items()}
    if str(lowered.get("x-cos-object-type") or "normal").lower() != "normal" or lowered.get("x-cos-symlink-target"):
        raise ValueError("COS appendable or symbolic-link objects cannot be safely copied")
    if any(lowered.get(name) for name in (
        "x-cos-object-lock-mode", "x-cos-object-lock-retain-until-date", "x-cos-object-lock-legal-hold",
        "x-cos-server-side-encryption-customer-algorithm",
    )):
        raise ValueError("COS objects with retention, legal hold or customer keys require a separate migration")
    fields = (
        "content-type", "cache-control", "content-disposition", "content-encoding", "content-language", "expires",
        "x-cos-server-side-encryption", "x-cos-server-side-encryption-cos-kms-key-id",
        "x-cos-server-side-encryption-context",
    )
    attributes = {name: str(lowered[name]) for name in fields if lowered.get(name) is not None}
    attributes["x-cos-storage-class"] = str(lowered.get("x-cos-storage-class") or "STANDARD")
    acl = client.get_object_acl(Bucket=bucket, Key=key)
    mode, grants = _acl_parts(acl)
    response = client.get_object_tagging(Bucket=bucket, Key=key)
    tag_set = response.get("TagSet") or {}
    tags = []
    for item in one_or_many(tag_set.get("Tag")):
        if not isinstance(item.get("Key"), str) or not item["Key"]:
            raise ValueError("COS object tags are incomplete")
        tags.append((item["Key"], str(item.get("Value") or "")))
    if len({key for key, _ in tags}) != len(tags):
        raise ValueError("COS object tags are ambiguous")
    return CopySnapshot(
        size=int(raw_size), etag=etag, modified=modified,
        crc64=header(head, "x-cos-hash-crc64ecma"), content_md5=header(head, "content-md5"),
        attributes=tuple(sorted(attributes.items())), metadata=tuple(sorted(metadata(head).items())),
        acl_mode=mode, acl_owner=str(acl["Owner"]["ID"]), acl_grants=grants, tags=tuple(sorted(tags)),
    )
