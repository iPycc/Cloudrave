"""Durable physical-object cleanup tasks."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import precise_datetime as DateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


class GcTask(Base):
    __tablename__ = "gc_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("storage_policies.id", ondelete="CASCADE"), index=True)
    mount_id: Mapped[int] = mapped_column(ForeignKey("bucket_mounts.id", ondelete="CASCADE"), index=True)
    blob_path: Mapped[str] = mapped_column(String(1024))
    physical_id: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
