"""Import ORM tables so metadata is registered."""

from app.models.tables import (
    AuditLog,
    BucketMount,
    Node,
    ObjectRecord,
    PasskeyCeremony,
    PasskeyCredential,
    Session,
    StoragePolicy,
    SystemMeta,
    UploadPartRecord,
    UploadSessionRecord,
    User,
)

__all__ = [
    "AuditLog",
    "BucketMount",
    "Node",
    "ObjectRecord",
    "PasskeyCeremony",
    "PasskeyCredential",
    "Session",
    "StoragePolicy",
    "SystemMeta",
    "UploadPartRecord",
    "UploadSessionRecord",
    "User",
]
