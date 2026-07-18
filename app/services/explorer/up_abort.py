"""Provider cleanup for canceled upload sessions."""

from __future__ import annotations

from app.models.enums import UploadMode
from app.storage.call import call_adapter
from app.storage.errors import is_not_found


async def cleanup_upload(adapter, policy, mount, upload, mode: UploadMode) -> None:
    if mode == UploadMode.SINGLE:
        await call_adapter(adapter, "delete_object", policy, mount, upload.object_key, hard_delete=True)
        return

    if await _has_object(adapter, policy, mount, upload.object_key):
        await call_adapter(adapter, "delete_object", policy, mount, upload.object_key, hard_delete=True)
        return

    try:
        await call_adapter(adapter, "abort_multipart_upload", policy, mount, upload)
    except Exception as exc:
        # CompleteMultipart may have won a race after the first HEAD.
        if await _has_object(adapter, policy, mount, upload.object_key):
            await call_adapter(adapter, "delete_object", policy, mount, upload.object_key, hard_delete=True)
        elif not is_not_found(exc):
            raise


async def _has_object(adapter, policy, mount, object_key: str) -> bool:
    try:
        await call_adapter(adapter, "head_object", policy, mount, object_key)
    except Exception as exc:
        if is_not_found(exc):
            return False
        raise
    return True
