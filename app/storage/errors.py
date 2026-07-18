"""Provider-neutral storage error classification."""

from __future__ import annotations


def is_not_found(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    code_getter = getattr(exc, "get_error_code", None)
    code = str(code_getter() if callable(code_getter) else "")
    if code in {"NoSuchKey", "NoSuchObject", "NoSuchResource", "NoSuchUpload", "NotFound", "404"}:
        return True
    status_getter = getattr(exc, "get_status_code", None)
    status = status_getter() if callable(status_getter) else None
    return str(status) == "404"
