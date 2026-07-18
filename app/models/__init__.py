"""Import ORM tables so metadata is registered."""

from app.models.blob import BlobRef
from app.models.gc import GcTask
from app.models.shared import ShareMount
from app.models.tables import (
    AuditLog,
    BucketMount,
    Node,
    ObjectRecord,
    PasskeyCeremony,
    PasskeyCredential,
    Session,
    ShareItem,
    ShareRecord,
    StoragePolicy,
    SystemMeta,
    UploadPartRecord,
    UploadSessionRecord,
    User,
)

__all__ = [
    "AuditLog",
    "BucketMount",
    "BlobRef",
    "GcTask",
    "Node",
    "ObjectRecord",
    "PasskeyCeremony",
    "PasskeyCredential",
    "Session",
    "ShareItem",
    "ShareMount",
    "ShareRecord",
    "StoragePolicy",
    "SystemMeta",
    "UploadPartRecord",
    "UploadSessionRecord",
    "User",
]
