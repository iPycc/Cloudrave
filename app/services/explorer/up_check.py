"""Upload sizing and part validation."""

from __future__ import annotations

from math import ceil
from pathlib import Path

from fastapi import HTTPException, status

from app.models.enums import StoragePolicyType, UploadMode
from app.storage.limits import MAX_OBJECT_SIZE, MAX_PARTS, MAX_PART_SIZE, MAX_SINGLE_PUT, MIB
from app.storage.types import MultipartUploadState


def choose_part_size(size: int, requested: int | None, policy_size_mb: int) -> int:
    if size > MAX_OBJECT_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="File exceeds the COS multipart object limit",
        )
    if requested is not None and requested > MAX_PART_SIZE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Upload part size cannot exceed 5 GiB",
        )
    part_size = requested or policy_size_mb * MIB
    result = max(part_size, MIB, ceil(max(size, 1) / MAX_PARTS))
    if result > MAX_PART_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="File cannot be represented within the COS multipart limits",
        )
    return result


def choose_mode(
    mode: UploadMode,
    provider: StoragePolicyType,
    size: int,
    threshold_mb: int,
) -> UploadMode:
    if provider == StoragePolicyType.LOCAL_FS:
        return UploadMode.MULTIPART
    if mode == UploadMode.AUTO:
        threshold = max(1, threshold_mb) * MIB
        return UploadMode.SINGLE if size <= min(threshold, MAX_SINGLE_PUT) else UploadMode.MULTIPART
    if mode == UploadMode.SINGLE and size > MAX_SINGLE_PUT:
        return UploadMode.MULTIPART
    return mode


def apply_threshold(
    part_size: int,
    provider: StoragePolicyType,
    size: int,
    threshold_mb: int,
) -> int:
    if provider == StoragePolicyType.LOCAL_FS and size <= max(1, threshold_mb) * MIB:
        return max(size, 1)
    return part_size


def part_count(size: int, part_size: int) -> int:
    return max(1, ceil(size / part_size)) if size else 1


def validate_part_count(count: int) -> None:
    if count > MAX_PARTS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="File requires too many upload parts",
        )


def validate_part_number(record, part_number: int) -> None:
    if part_number < 1 or part_number > part_count(record.size, record.part_size):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid upload part number")


def expected_part_size(record, part_number: int) -> int:
    validate_part_number(record, part_number)
    count = part_count(record.size, record.part_size)
    if record.size == 0:
        return 0
    if part_number < count:
        return record.part_size
    return record.size - record.part_size * (count - 1)


def validate_complete_parts(record, parts) -> None:
    count = part_count(record.size, record.part_size)
    by_number = {part.part_number: part for part in parts}
    if set(by_number) != set(range(1, count + 1)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Upload parts are incomplete")
    for part_number, part in by_number.items():
        if not getattr(part, "etag", None):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Upload part ETag is missing")
        size = getattr(part, "size", None)
        if size is not None and size != expected_part_size(record, part_number):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Upload part size is invalid")


def build_upload_state(record, policy, mount, storage_manager) -> MultipartUploadState:
    adapter = storage_manager.get_adapter(policy)
    mount_root = adapter.prepare_mount_root(mount)
    upload_id = record.upload_id or record.id
    return MultipartUploadState(
        upload_id=upload_id,
        provider_type=policy.provider.value,
        mount_id=str(mount.id),
        object_key=record.path,
        upload_root=mount_root,
        upload_dir=Path(mount_root) / ".multipart" / upload_id,
        part_size_bytes=record.part_size,
        total_size=record.size,
        provider_upload_id=record.upload_id,
    )
