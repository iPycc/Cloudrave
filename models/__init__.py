"""Import ORM tables so metadata is registered."""

from models.blob import BlobRef
from models.gc import GcTask
from models.shared import ShareMount
from db.mig.row import SchemaMigration
from models.tables import (
    AuditLog,
    BucketMount,
    Node,
    ObjectRecord,
    OAuthIdentity,
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
    "OAuthIdentity",
    "PasskeyCeremony",
    "PasskeyCredential",
    "Session",
    "ShareItem",
    "ShareMount",
    "SchemaMigration",
    "ShareRecord",
    "StoragePolicy",
    "SystemMeta",
    "UploadPartRecord",
    "UploadSessionRecord",
    "User",
]
