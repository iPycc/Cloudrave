"""Database enums used across Cloudrave models."""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"
    GUEST = "guest"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class StoragePolicyType(StrEnum):
    """Deprecated value constants; provider validation now lives in DriveRegistry."""

    LOCAL_FS = "local_fs"
    TENCENT_COS = "tencent_cos"
    ALIYUN_OSS = "aliyun_oss"


class NodeType(StrEnum):
    FOLDER = "folder"
    FILE = "file"


class NodeStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


class UploadMode(StrEnum):
    AUTO = "auto"
    SINGLE = "single_put"
    MULTIPART = "multipart"


class UploadState(StrEnum):
    CREATED = "created"
    UPLOADING = "uploading"
    COMPLETING = "completing"
    ABORTING = "aborting"
    COMPLETED = "completed"
    ABORTED = "aborted"
    EXPIRED = "expired"
    FAILED = "failed"


class ShareAccess(StrEnum):
    PUBLIC = "public"
    PASSWORD = "password"
