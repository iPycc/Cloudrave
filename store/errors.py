"""Provider-neutral storage error classification."""

from __future__ import annotations

from collections.abc import Mapping


def unwrap_storage_error(exc: Exception) -> Exception:
    """Unwrap provider SDK operation errors without importing an SDK."""

    current = exc
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        unwrap = getattr(current, "unwrap", None)
        nested = unwrap() if callable(unwrap) else None
        if not isinstance(nested, Exception):
            break
        current = nested
    return current


def storage_error_code(exc: Exception) -> str:
    current = unwrap_storage_error(exc)
    digest_getter = getattr(current, "get_digest_msg", None)
    if callable(digest_getter):
        digest = digest_getter()
        return str(digest.get("code") or digest.get("Code") or "") if isinstance(digest, Mapping) else ""
    code_getter = getattr(current, "get_error_code", None)
    value = code_getter() if callable(code_getter) else getattr(current, "code", None)
    return str(value or "")


def storage_error_details(exc: Exception) -> dict[str, str]:
    current = unwrap_storage_error(exc)
    digest_getter = getattr(current, "get_digest_msg", None)
    if callable(digest_getter):
        digest = digest_getter()
        message = (digest.get("message") or digest.get("Message")) if isinstance(digest, Mapping) else None
    else:
        message = getattr(current, "message", None) if storage_error_code(current) else None
    status_getter = getattr(current, "get_status_code", None)
    status = status_getter() if callable(status_getter) else getattr(current, "status_code", None)
    return {
        "message": str(message or "存储请求失败"),
        "code": storage_error_code(current) or str(status or "storage_error"),
    }


def is_not_found(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    current = unwrap_storage_error(exc)
    code = storage_error_code(current)
    if code in {"NoSuchKey", "NoSuchObject", "NoSuchResource", "NoSuchUpload", "NotFound", "404"}:
        return True
    status_getter = getattr(current, "get_status_code", None)
    status = status_getter() if callable(status_getter) else getattr(current, "status_code", None)
    return str(status) == "404"
