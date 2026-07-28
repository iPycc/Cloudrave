"""COS presigning and multipart-upload operations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, BinaryIO

from store.types import MultipartPart, MultipartUploadState, PresignedURL, StorageObject, utcnow
from store.cos.sec import (
    assert_encryption,
    encryption_args,
    metadata_headers,
    response_params,
    signed_encryption_headers,
)


class CosUploadMixin:
    def presign_put(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> PresignedURL:
        ttl = self._ttl(policy)
        headers = signed_encryption_headers(policy)
        if mime_type:
            headers["Content-Type"] = mime_type
        headers.update(metadata_headers(metadata))
        url = self._client_for(policy).get_presigned_url(
            Method="PUT",
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, object_key),
            Expired=ttl,
            # The SDK mutates this dictionary by injecting Authorization.
            # Never leak that internal signing header back to the browser.
            Headers=dict(headers),
        )
        return PresignedURL(
            method="PUT",
            url=url,
            headers=headers,
            expires_at=utcnow() + self._seconds(ttl),
        )

    async def apresign_put(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> PresignedURL:
        return await asyncio.to_thread(
            self.presign_put,
            policy,
            mount,
            object_key,
            mime_type=mime_type,
            metadata=metadata,
        )

    def create_multipart_upload(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        total_size: int | None = None,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MultipartUploadState:
        kwargs: dict[str, Any] = encryption_args(policy)
        if mime_type:
            kwargs["ContentType"] = mime_type
        if metadata:
            kwargs["Metadata"] = metadata_headers(metadata)
        response = self._client_for(policy).create_multipart_upload(
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, object_key),
            **kwargs,
        )
        upload_id = str(response.get("UploadId", ""))
        if not upload_id:
            raise RuntimeError("COS did not return an UploadId")
        ttl = self._ttl(policy)
        prefix = self._mount_prefix(policy, mount)
        return MultipartUploadState(
            upload_id=upload_id,
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=object_key,
            upload_root=Path(prefix),
            upload_dir=Path(prefix) / ".multipart" / upload_id,
            part_size_bytes=int(policy.part_size_mb) * 1024 * 1024,
            total_size=total_size,
            metadata=metadata or {},
            expires_at=utcnow() + self._seconds(ttl),
            provider_upload_id=upload_id,
        )

    async def acreate_multipart_upload(self, policy: Any, mount: Any, object_key: str, **kwargs: Any) -> MultipartUploadState:
        return await asyncio.to_thread(self.create_multipart_upload, policy, mount, object_key, **kwargs)

    def presign_part(self, policy: Any, mount: Any, upload: MultipartUploadState, part_number: int) -> PresignedURL:
        self._validate_part_number(part_number)
        ttl = self._ttl(policy)
        url = self._client_for(policy).get_presigned_url(
            Method="PUT",
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, upload.object_key),
            Expired=ttl,
            Params={"partNumber": str(part_number), "uploadId": upload.upload_id},
        )
        return PresignedURL(
            method="PUT",
            url=url,
            expires_at=utcnow() + self._seconds(ttl),
            part_number=part_number,
            upload_id=upload.upload_id,
        )

    async def apresign_part(self, policy: Any, mount: Any, upload: MultipartUploadState, part_number: int) -> PresignedURL:
        return await asyncio.to_thread(self.presign_part, policy, mount, upload, part_number)

    def upload_part(
        self,
        policy: Any,
        mount: Any,
        upload: MultipartUploadState,
        part_number: int,
        body: bytes | BinaryIO,
    ) -> MultipartPart:
        self._validate_part_number(part_number)
        kwargs: dict[str, Any] = {}
        if isinstance(body, bytes):
            kwargs["ContentLength"] = len(body)
        response = self._client_for(policy).upload_part(
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, upload.object_key),
            PartNumber=part_number,
            UploadId=upload.upload_id,
            Body=body,
            **kwargs,
        )
        etag = str(response.get("ETag", ""))
        if not etag:
            raise RuntimeError("COS did not return an ETag for the uploaded part")
        return MultipartPart(
            part_number=part_number,
            etag=etag,
            size=len(body) if isinstance(body, bytes) else None,
        )

    async def aupload_part(self, policy: Any, mount: Any, upload: MultipartUploadState, part_number: int, body: bytes | BinaryIO) -> MultipartPart:
        return await asyncio.to_thread(self.upload_part, policy, mount, upload, part_number, body)

    def list_parts(self, policy: Any, mount: Any, upload: MultipartUploadState) -> list[MultipartPart]:
        marker = 0
        result: list[MultipartPart] = []
        while True:
            response = self._client_for(policy).list_parts(
                Bucket=self._bucket(policy),
                Key=self._key(policy, mount, upload.object_key),
                UploadId=upload.upload_id,
                MaxParts=1000,
                PartNumberMarker=marker,
            )
            raw_parts = response.get("Part", [])
            if isinstance(raw_parts, dict):
                raw_parts = [raw_parts]
            for item in raw_parts:
                result.append(
                    MultipartPart(
                        part_number=int(item["PartNumber"]),
                        etag=str(item["ETag"]),
                        size=int(item["Size"]) if item.get("Size") is not None else None,
                    )
                )
            if str(response.get("IsTruncated", "false")).lower() != "true":
                return result
            next_marker = int(response.get("NextPartNumberMarker") or 0)
            if next_marker <= marker:
                raise RuntimeError("COS returned a truncated part page without a usable marker")
            marker = next_marker

    async def alist_parts(self, policy: Any, mount: Any, upload: MultipartUploadState) -> list[MultipartPart]:
        return await asyncio.to_thread(self.list_parts, policy, mount, upload)

    def complete_multipart_upload(
        self,
        policy: Any,
        mount: Any,
        upload: MultipartUploadState,
        parts: list[MultipartPart],
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StorageObject:
        if not parts:
            raise ValueError("at least one multipart part is required")
        ordered = sorted(parts, key=lambda part: part.part_number)
        seen: set[int] = set()
        for part in ordered:
            self._validate_part_number(part.part_number)
            if part.part_number in seen:
                raise ValueError("multipart part numbers must be unique")
            if not part.etag:
                raise ValueError("every multipart part requires an ETag")
            seen.add(part.part_number)
        self._client_for(policy).complete_multipart_upload(
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, upload.object_key),
            UploadId=upload.upload_id,
            MultipartUpload={
                "Part": [
                    {"PartNumber": part.part_number, "ETag": part.etag}
                    for part in ordered
                ]
            },
        )
        completed = self.head_object(policy, mount, upload.object_key, verify_encryption=True)
        if upload.total_size is not None and completed.size != upload.total_size:
            raise RuntimeError(
                f"COS multipart size mismatch: expected {upload.total_size}, received {completed.size}"
            )
        completed.mime_type = completed.mime_type or mime_type
        completed.metadata = completed.metadata or metadata or upload.metadata
        return completed

    async def acomplete_multipart_upload(self, policy: Any, mount: Any, upload: MultipartUploadState, parts: list[MultipartPart], **kwargs: Any) -> StorageObject:
        return await asyncio.to_thread(self.complete_multipart_upload, policy, mount, upload, parts, **kwargs)

    def abort_multipart_upload(self, policy: Any, mount: Any, upload: MultipartUploadState) -> bool:
        self._client_for(policy).abort_multipart_upload(
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, upload.object_key),
            UploadId=upload.upload_id,
        )
        return True

    async def aabort_multipart_upload(self, policy: Any, mount: Any, upload: MultipartUploadState) -> bool:
        return await asyncio.to_thread(self.abort_multipart_upload, policy, mount, upload)

    def build_download_url(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        expires_in: int | None = None,
        download_name: str | None = None,
        inline: bool = False,
    ) -> PresignedURL:
        ttl = self._ttl(policy) if expires_in is None else expires_in
        if ttl <= 0:
            raise ValueError("expires_in must be positive")
        url = self._client_for(policy).get_presigned_url(
            Method="GET",
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, object_key),
            Expired=ttl,
            Params=response_params(download_name, inline),
        )
        return PresignedURL(method="GET", url=url, expires_at=utcnow() + self._seconds(ttl))

    async def abuild_download_url(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        expires_in: int | None = None,
        download_name: str | None = None,
        inline: bool = False,
    ) -> PresignedURL:
        return await asyncio.to_thread(
            self.build_download_url,
            policy,
            mount,
            object_key,
            expires_in=expires_in,
            download_name=download_name,
            inline=inline,
        )

    @staticmethod
    def _validate_part_number(part_number: int) -> None:
        if not 1 <= part_number <= 10_000:
            raise ValueError("COS multipart part_number must be between 1 and 10000")
