"""Migration ledger model."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import precise_datetime as DateTime


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
