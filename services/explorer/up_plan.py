"""Upload session plan construction."""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from models.enums import UploadMode
from schemas.upload import UploadPartPlan, UploadSessionPlan
from services.explorer.oss_upload_errors import call_upload_storage
from services.explorer.up_check import apply_threshold, choose_mode, choose_part_size, part_count, validate_part_count
from store.errors import is_not_found


PRESIGN_WINDOW = 3
PATH_TOKEN_METADATA = "cloudrave-upload-token"
LOGGER = logging.getLogger("cloudrave.upload")


async def _abort_unplanned_upload(adapter, policy, mount, upload) -> None:
    try:
        await call_upload_storage(adapter, "abort_multipart_upload", policy, mount, upload)
    except Exception as exc:
        if not is_not_found(exc):
            reason = exc.detail if isinstance(exc, HTTPException) else type(exc).__name__
            LOGGER.warning("Could not abort an unplanned multipart upload for mount %s: %s", mount.id, reason)


def _window(extra: dict) -> int:
    try:
        value = int(extra.get("presign_window", extra.get("concurrency", PRESIGN_WINDOW)))
    except (TypeError, ValueError):
        value = PRESIGN_WINDOW
    return max(1, min(value, 64))


async def sign_part(adapter, policy, mount, upload, part_number: int, *, relay_url: str | None = None) -> UploadPartPlan:
    presigned = await call_upload_storage(adapter, "presign_part", policy, mount, upload, part_number)
    return UploadPartPlan(
        part_number=part_number,
        method=presigned.method,
        url=presigned.url,
        headers=presigned.headers,
        expires_at=presigned.expires_at,
        body_mode=presigned.body_mode,
        form_fields=presigned.form_fields,
        file_field=presigned.file_field,
        record_etag=presigned.record_etag,
        response_mode=presigned.response_mode,
        relay_url=relay_url,
    )


async def sign_put(
    adapter, policy, mount, object_key: str, mime_type: str | None, checksum: str | None,
    *, path_token: str | None = None,
) -> UploadPartPlan:
    metadata = {"sha256": checksum} if checksum else {}
    guarded = {}
    if path_token:
        metadata[PATH_TOKEN_METADATA] = path_token
        guarded["no_overwrite"] = True
    try:
        presigned = await call_upload_storage(
            adapter, "presign_put", policy, mount, object_key,
            mime_type=mime_type, metadata=metadata or None, **guarded,
        )
    except ValueError as exc:
        if path_token:
            raise HTTPException(status_code=409, detail="存储桶不支持防覆盖上传，请检查版本控制配置") from exc
        raise
    return UploadPartPlan(
        part_number=1,
        method=presigned.method,
        url=presigned.url,
        headers=presigned.headers,
        expires_at=presigned.expires_at,
    )


async def make_plan(*, uploads, storage_manager, policy, mount, parent, file_name, object_key, payload, path_token=None, idempotency_key=None, request_hash=None):
    adapter = storage_manager.get_adapter(policy)
    capability = adapter.capabilities
    size = apply_threshold(
        choose_part_size(payload.size, payload.part_size, policy.part_size_mb, capability),
        capability,
        payload.size,
        policy.multipart_threshold_mb,
    )
    count = part_count(payload.size, size)
    validate_part_count(count, capability)
    mode = choose_mode(
        payload.mode,
        capability,
        payload.size,
        policy.multipart_threshold_mb,
    )

    if capability.supports_single_put and mode == UploadMode.SINGLE:
        single_size = payload.size or 1
        put = await sign_put(adapter, policy, mount, object_key, payload.content_type, payload.checksum, path_token=path_token)
        record = await uploads.create_session(
            owner_id=mount.owner_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            mount_id=mount.id,
            node_id=parent.id if parent else None,
            path=object_key,
            path_token=path_token,
            mode=mode,
            upload_id=None,
            file_name=file_name,
            content_type=payload.content_type,
            size=payload.size,
            part_size=single_size,
            checksum=payload.checksum,
            provider_checksum=payload.provider_checksum,
        )
        return record, UploadSessionPlan(
            session_id=record.id,
            upload_mode=mode,
            object_key=object_key,
            part_size=single_size,
            part_count=1,
            upload_urls=[put],
            expires_at=put.expires_at,
            transport=capability.upload_transport,
            required_response_headers=[],
        )

    metadata = {
        "sha256": payload.checksum or "",
        "provider_checksum": payload.provider_checksum or "",
        "file_name": file_name,
        "part_size": str(size),
    }
    guarded = {}
    if path_token:
        metadata[PATH_TOKEN_METADATA] = path_token
        guarded["no_overwrite"] = True
    try:
        upload = await call_upload_storage(
            adapter,
            "create_multipart_upload",
            policy,
            mount,
            object_key,
            total_size=payload.size,
            mime_type=payload.content_type,
            metadata=metadata,
            **guarded,
        )
    except ValueError as exc:
        if path_token:
            raise HTTPException(status_code=409, detail="存储桶不支持防覆盖上传，请检查版本控制配置") from exc
        raise
    try:
        record = await uploads.create_session(
            owner_id=mount.owner_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            mount_id=mount.id,
            node_id=parent.id if parent else None,
            path=object_key,
            path_token=path_token,
            mode=UploadMode.MULTIPART,
            upload_id=upload.upload_id,
            file_name=file_name,
            content_type=payload.content_type,
            size=payload.size,
            part_size=size,
            checksum=payload.checksum,
            provider_checksum=payload.provider_checksum,
        )
    except BaseException:
        await asyncio.shield(_abort_unplanned_upload(adapter, policy, mount, upload))
        raise
    extra = policy.extra or {}
    window = (
        0
        if capability.part_checksum_algorithm or not capability.eager_part_plans
        else min(count, _window(extra))
    )
    if capability.upload_transport == "cloudrave":
        urls = [
            UploadPartPlan(
                part_number=number,
                method="POST",
                url=f"/api/v1/explorer/upload/{record.id}/part",
            )
            for number in range(1, window + 1)
        ]
    else:
        relay_url = (
            f"/api/v1/explorer/upload/{record.id}/part"
            if getattr(adapter, "supports_relay_upload", False)
            else None
        )
        try:
            results = await asyncio.gather(
                *(sign_part(adapter, policy, mount, upload, number, relay_url=relay_url) for number in range(1, window + 1)),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            urls = results
        except BaseException:
            await asyncio.shield(_abort_unplanned_upload(adapter, policy, mount, upload))
            raise
    return record, UploadSessionPlan(
        session_id=record.id,
        upload_mode=UploadMode.MULTIPART,
        upload_id=upload.upload_id,
        object_key=object_key,
        part_size=size,
        part_count=count,
        upload_urls=urls,
        expires_at=upload.expires_at,
        transport=capability.upload_transport,
        required_response_headers=["ETag"] if capability.requires_part_etag else [],
        part_checksum_algorithm=capability.part_checksum_algorithm,
    )
