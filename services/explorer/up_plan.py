"""Upload session plan construction."""

from __future__ import annotations

import asyncio

from models.enums import UploadMode
from schemas.upload import UploadPartPlan, UploadSessionPlan
from services.explorer.up_check import apply_threshold, choose_mode, choose_part_size, part_count, validate_part_count
from store.call import call_adapter


PRESIGN_WINDOW = 3


def _window(extra: dict) -> int:
    try:
        value = int(extra.get("presign_window", extra.get("concurrency", PRESIGN_WINDOW)))
    except (TypeError, ValueError):
        value = PRESIGN_WINDOW
    return max(1, min(value, 64))


async def sign_part(adapter, policy, mount, upload, part_number: int) -> UploadPartPlan:
    presigned = await call_adapter(adapter, "presign_part", policy, mount, upload, part_number)
    return UploadPartPlan(
        part_number=part_number,
        method=presigned.method,
        url=presigned.url,
        headers=presigned.headers,
        expires_at=presigned.expires_at,
    )


async def sign_put(adapter, policy, mount, object_key: str, mime_type: str | None, checksum: str | None) -> UploadPartPlan:
    presigned = await call_adapter(
        adapter,
        "presign_put",
        policy,
        mount,
        object_key,
        mime_type=mime_type,
        metadata={"sha256": checksum} if checksum else None,
    )
    return UploadPartPlan(
        part_number=1,
        method=presigned.method,
        url=presigned.url,
        headers=presigned.headers,
        expires_at=presigned.expires_at,
    )


async def make_plan(*, uploads, storage_manager, policy, mount, parent, file_name, object_key, payload):
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
        put = await sign_put(adapter, policy, mount, object_key, payload.content_type, payload.checksum)
        record = await uploads.create_session(
            owner_id=mount.owner_id,
            mount_id=mount.id,
            node_id=parent.id if parent else None,
            path=object_key,
            mode=mode,
            upload_id=None,
            file_name=file_name,
            content_type=payload.content_type,
            size=payload.size,
            part_size=single_size,
            checksum=payload.checksum,
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

    upload = await call_adapter(
        adapter,
        "create_multipart_upload",
        policy,
        mount,
        object_key,
        total_size=payload.size,
        mime_type=payload.content_type,
        metadata={"sha256": payload.checksum} if payload.checksum else None,
    )
    record = await uploads.create_session(
        owner_id=mount.owner_id,
        mount_id=mount.id,
        node_id=parent.id if parent else None,
        path=object_key,
        mode=UploadMode.MULTIPART,
        upload_id=upload.upload_id,
        file_name=file_name,
        content_type=payload.content_type,
        size=payload.size,
        part_size=size,
        checksum=payload.checksum,
    )
    extra = policy.extra or {}
    window = min(count, _window(extra))
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
        urls = await asyncio.gather(
            *(sign_part(adapter, policy, mount, upload, number) for number in range(1, window + 1))
        )
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
    )
