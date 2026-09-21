"""ORM table declarations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Index, JSON, Boolean, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.enums import (
    NodeStatus,
    NodeType,
    SessionStatus,
    ShareAccess,
    UploadMode,
    UploadState,
    UserRole,
    UserStatus,
)
from db.base import Base
from db.types import precise_datetime as DateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SystemMeta(Base):
    __tablename__ = "system_meta"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    avatar_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False), default=UserRole.USER)
    status: Mapped[UserStatus] = mapped_column(Enum(UserStatus, native_enum=False), default=UserStatus.ACTIVE)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    thumbnails_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    password_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    totp_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_backup_codes: Mapped[list[str]] = mapped_column(JSON, default=list)

    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    mounts: Mapped[list["BucketMount"]] = relationship(back_populates="owner")
    passkeys: Mapped[list["PasskeyCredential"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    passkey_ceremonies: Mapped[list["PasskeyCeremony"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    oauth_identities: Mapped[list["OAuthIdentity"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    shares: Mapped[list["ShareRecord"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(255))
    token_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus, native_enum=False), default=SessionStatus.ACTIVE)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (Index("ix_sessions_user_status", "user_id", "status"),)


class OAuthIdentity(Base, TimestampMixin):
    __tablename__ = "oauth_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(255))
    provider_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="oauth_identities")

    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_oauth_identities_provider_subject"),
        UniqueConstraint("user_id", "provider", name="uq_oauth_identities_user_provider"),
    )


class PasskeyCredential(Base, TimestampMixin):
    __tablename__ = "passkey_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    credential_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    public_key: Mapped[str] = mapped_column(Text)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    aaguid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transports: Mapped[list[str]] = mapped_column(JSON, default=list)

    user: Mapped[User] = relationship(back_populates="passkeys")


class PasskeyCeremony(Base):
    __tablename__ = "passkey_ceremonies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    ceremony_type: Mapped[str] = mapped_column(String(32), index=True)
    challenge: Mapped[str] = mapped_column(String(255))
    email_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User | None] = relationship(back_populates="passkey_ceremonies")


class StoragePolicy(Base, TimestampMixin):
    __tablename__ = "storage_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    provider: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    bucket_name: Mapped[str] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_prefix: Mapped[str] = mapped_column(String(255), default="")
    secret_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credential_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_version: Mapped[int] = mapped_column(Integer, default=1)
    multipart_threshold_mb: Mapped[int] = mapped_column(Integer, default=64)
    part_size_mb: Mapped[int] = mapped_column(Integer, default=16)
    presign_ttl_seconds: Mapped[int] = mapped_column(Integer, default=900)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    cors_auto_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    mounts: Mapped[list["BucketMount"]] = relationship(back_populates="policy", cascade="all, delete-orphan")


class BucketMount(Base, TimestampMixin):
    __tablename__ = "bucket_mounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("storage_policies.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    mount_slug: Mapped[str] = mapped_column(String(128))
    root_path: Mapped[str] = mapped_column(String(255), default="/")
    provider_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    quota_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    owner: Mapped[User] = relationship(back_populates="mounts")
    policy: Mapped[StoragePolicy] = relationship(back_populates="mounts")
    nodes: Mapped[list["Node"]] = relationship(back_populates="mount", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_bucket_mount_owner_name"),
        UniqueConstraint("owner_id", "mount_slug", name="uq_bucket_mount_owner_slug"),
    )

    @property
    def mode(self) -> str:
        """Return the mount ownership mode stored in the compatible JSON field."""

        value = (self.extra or {}).get("mode", "managed")
        return value if value in {"managed", "mirror"} else "managed"

    @property
    def read_only(self) -> bool:
        return bool((self.extra or {}).get("read_only", False))

    @property
    def sync_status(self) -> str:
        return str(((self.extra or {}).get("_sync") or {}).get("status", "never"))

    @property
    def last_sync_at(self) -> str | None:
        value = ((self.extra or {}).get("_sync") or {}).get("completed_at")
        return str(value) if value else None

    @property
    def sync_error(self) -> str | None:
        value = ((self.extra or {}).get("_sync") or {}).get("error")
        return str(value) if value else None

    @property
    def synced_objects(self) -> int:
        value = ((self.extra or {}).get("_sync") or {}).get("synced_objects", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


class Node(Base, TimestampMixin):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    mount_id: Mapped[int] = mapped_column(ForeignKey("bucket_mounts.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True)
    type: Mapped[NodeType] = mapped_column(Enum(NodeType, native_enum=False))
    status: Mapped[NodeStatus] = mapped_column(Enum(NodeStatus, native_enum=False), default=NodeStatus.ACTIVE)
    name: Mapped[str] = mapped_column(String(255))
    blob_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    mount: Mapped[BucketMount] = relationship(back_populates="nodes")

    __table_args__ = (
        UniqueConstraint("mount_id", "parent_id", "name", name="uq_nodes_mount_parent_name"),
        Index("ix_nodes_mount_parent_deleted", "mount_id", "parent_id", "deleted_at"),
        Index("ix_nodes_owner_deleted", "owner_id", "deleted_at"),
    )


class ObjectRecord(Base, TimestampMixin):
    __tablename__ = "objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), unique=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("storage_policies.id", ondelete="CASCADE"))
    blob_path: Mapped[str] = mapped_column(String(1024))
    physical_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class PreviewAnnotation(Base, TimestampMixin):
    """Private, per-user sidecar data for non-destructive preview annotations."""

    __tablename__ = "preview_annotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    source_version: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[list[dict]] = mapped_column(JSON, default=list)

    __table_args__ = (
        UniqueConstraint("owner_id", "node_id", name="uq_preview_annotations_owner_node"),
    )


class ShareRecord(Base, TimestampMixin):
    __tablename__ = "shares"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    access: Mapped[ShareAccess] = mapped_column(Enum(ShareAccess, native_enum=False), default=ShareAccess.PUBLIC)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_downloads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    owner: Mapped[User] = relationship(back_populates="shares")
    node: Mapped[Node] = relationship()
    items: Mapped[list["ShareItem"]] = relationship(
        back_populates="share",
        cascade="all, delete-orphan",
        order_by="ShareItem.position",
    )


class ShareItem(Base):
    """A root node exposed by one share link.

    ``ShareRecord.node_id`` remains the primary item for backwards compatibility;
    new multi-selection shares keep every selected root in this table.
    """

    __tablename__ = "share_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    share_id: Mapped[str] = mapped_column(ForeignKey("shares.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    share: Mapped[ShareRecord] = relationship(back_populates="items")
    node: Mapped[Node] = relationship()

    __table_args__ = (UniqueConstraint("share_id", "node_id", name="uq_share_items_share_node"),)


class UploadSessionRecord(Base, TimestampMixin):
    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    mount_id: Mapped[int] = mapped_column(ForeignKey("bucket_mounts.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    path: Mapped[str] = mapped_column(String(1024))
    mode: Mapped[UploadMode] = mapped_column(Enum(UploadMode, native_enum=False))
    state: Mapped[UploadState] = mapped_column(Enum(UploadState, native_enum=False), default=UploadState.CREATED)
    upload_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    part_size: Mapped[int] = mapped_column(BigInteger, default=0)


class UploadPartRecord(Base):
    __tablename__ = "upload_parts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_session_id: Mapped[str] = mapped_column(ForeignKey("upload_sessions.id", ondelete="CASCADE"), index=True)
    part_number: Mapped[int] = mapped_column(Integer)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("upload_session_id", "part_number", name="uq_upload_parts_session_part"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(32))
    # Batch operations can reference thousands of node IDs. SQLite does not
    # enforce VARCHAR lengths, while MySQL does, so use a real long-text type.
    target: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_audit_logs_actor_created", "actor_id", "created_at"),)
