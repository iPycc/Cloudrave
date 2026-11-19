"""Compare remote identity before a protected copy, without treating ETag as a checksum."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from store.errors import is_not_found, storage_error_code


def exact_key_exists(get_symlink) -> bool:
    """Check the key itself, including links whose targets do not exist."""
    try:
        get_symlink()
    except Exception as exc:
        if storage_error_code(exc) == "NotSymlink":
            return True
        if is_not_found(exc):
            return False
        raise
    return True


def source_identity(size: Any, etag: Any, modified: Any) -> dict[str, str | int]:
    value = str(etag or "").strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if not value or len(value) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Source ETag is unavailable")
    if size is None or int(size) < 0:
        raise ValueError("Source size is unavailable")
    if isinstance(modified, datetime):
        timestamp = modified
    elif isinstance(modified, str) and modified:
        try:
            timestamp = datetime.fromisoformat(modified.replace("Z", "+00:00"))
        except ValueError:
            timestamp = parsedate_to_datetime(modified)
    else:
        raise ValueError("Source modification time is unavailable")
    if timestamp is None or timestamp.tzinfo is None:
        raise ValueError("Source modification time has no timezone")
    return {"size": int(size), "etag": value, "modified": timestamp.astimezone(UTC).isoformat()}


def require_source_identity(snapshot: Any, expected: dict[str, str | int] | None) -> None:
    if expected is None:
        return
    actual = source_identity(snapshot.size, snapshot.etag, snapshot.modified)
    if actual != expected:
        raise ValueError("Source identity changed after the path precheck")
