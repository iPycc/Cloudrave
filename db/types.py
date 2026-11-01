"""Shared SQL column types with consistent cross-backend precision."""

from __future__ import annotations

from sqlalchemy import DateTime as SQLDateTime
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime


def precise_datetime(*, timezone: bool = False):
    """Keep SQLite/PostgreSQL microseconds when storing time in MySQL."""
    return SQLDateTime(timezone=timezone).with_variant(MySQLDateTime(fsp=6), "mysql")
