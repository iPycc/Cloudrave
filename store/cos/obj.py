"""COS bucket and ordinary object operations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from math import ceil
from pathlib import Path
from typing import Any, BinaryIO

from store.types import MultipartPart, StorageObject, StorageObjectPage
from store.cos.parse import header, object_from_head, page_from_response
from store.cos.copy import CopySnapshot, read_copy_snapshot
from store.cos.sec import assert_encryption, encryption_args, metadata_headers
from store.cos.stream import AsyncCosStream, CosStream


class CosObjectMixin:
    def check_bucket(self, policy: Any, mount: Any) -> dict[str, Any]:
        return self._client_for(policy).head_bucket(Bucket=self._bucket(policy))

    async def acheck_bucket(self, policy: Any, mount: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.check_bucket, policy, mount)

    def validate_mount(self, policy: Any, mount: Any) -> dict[str, Any]:
        bucket_headers = self.check_bucket(policy, mount)
        sample = self.list_objects(policy, mount, max_keys=1)
        return {
            "bucket": self._bucket(policy),
            "region": self._region(policy),
            "prefix": self._mount_prefix(policy, mount),
            "bucket_headers": bucket_headers,
            "sample_count": len(sample.objects),
            "has_more": sample.is_truncated,
        }

    async def avalidate_mount(self, policy: Any, mount: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.validate_mount, policy, mount)

    def mount_has_objects(self, policy: Any, mount: Any) -> bool:
        client = self._client_for(policy)
        bucket = self._bucket(policy)
        prefix = self._list_prefix(policy, mount)
        response = client.list_objects(
            Bucket=bucket,
            Prefix=prefix,
            MaxKeys=1,
        )
        if response.get("Contents"):
            return True
        versions = client.list_objects_versions(Bucket=bucket, Prefix=prefix, MaxKeys=1)
        if versions.get("Version") or versions.get("DeleteMarker"):
            return True
        uploads = client.list_multipart_uploads(Bucket=bucket, Prefix=prefix, MaxUploads=1)
        return bool(uploads.get("Upload"))

    async def amount_has_objects(self, policy: Any, mount: Any) -> bool:
        return await asyncio.to_thread(self.mount_has_objects, policy, mount)

    def purge_mount(self, policy: Any, mount: Any) -> int:
        """Delete object versions, delete markers, and multipart uploads in scope."""

        client = self._client_for(policy)
        bucket = self._bucket(policy)
        prefix = self._list_prefix(policy, mount)
        deleted = self._purge_versions(client, bucket, prefix)
        deleted += self._purge_current_objects(client, bucket, prefix)
        deleted += self._purge_multipart_uploads(client, bucket, prefix)
        return deleted

    @staticmethod
    def _many(value: Any) -> list[dict[str, Any]]:
        if not value:
            return []
        return [value] if isinstance(value, dict) else list(value)

    def _purge_versions(self, client: Any, bucket: str, prefix: str) -> int:
        key_marker = ""
        version_marker = ""
        deleted = 0
        while True:
            response = client.list_objects_versions(
                Bucket=bucket,
                Prefix=prefix,
                KeyMarker=key_marker,
                VersionIdMarker=version_marker,
                MaxKeys=1000,
            )
            entries = self._many(response.get("Version")) + self._many(response.get("DeleteMarker"))
            targets = [
                {
                    "Key": str(item["Key"]),
                    **({"VersionId": str(item["VersionId"])} if item.get("VersionId") is not None else {}),
                }
                for item in entries
                if item.get("Key")
            ]
            deleted += self._delete_batch(client, bucket, targets)

            truncated = str(response.get("IsTruncated", "false")).lower() == "true"
            if not truncated:
                return deleted
            next_key = str(response.get("NextKeyMarker") or "")
            next_version = str(response.get("NextVersionIdMarker") or "")
            if not next_key or (next_key == key_marker and next_version == version_marker):
                raise RuntimeError("COS returned a truncated version page without usable markers")
            key_marker, version_marker = next_key, next_version

    def _purge_current_objects(self, client: Any, bucket: str, prefix: str) -> int:
        marker = ""
        deleted = 0
        while True:
            response = client.list_objects(
                Bucket=bucket,
                Prefix=prefix,
                Marker=marker,
                MaxKeys=1000,
            )
            contents = self._many(response.get("Contents"))
            keys = [str(item.get("Key", "")) for item in contents if item.get("Key")]
            deleted += self._delete_batch(client, bucket, [{"Key": key} for key in keys])

            truncated = str(response.get("IsTruncated", "false")).lower() == "true"
            if not truncated:
                return deleted
            next_marker = str(response.get("NextMarker") or (keys[-1] if keys else ""))
            if not next_marker or next_marker == marker:
                raise RuntimeError("COS returned a truncated page without a usable next marker")
            marker = next_marker

    def _purge_multipart_uploads(self, client: Any, bucket: str, prefix: str) -> int:
        key_marker = ""
        upload_marker = ""
        aborted = 0
        while True:
            response = client.list_multipart_uploads(
                Bucket=bucket,
                Prefix=prefix,
                KeyMarker=key_marker,
                UploadIdMarker=upload_marker,
                MaxUploads=1000,
            )
            uploads = self._many(response.get("Upload"))
            for upload in uploads:
                key = str(upload.get("Key") or "")
                upload_id = str(upload.get("UploadId") or "")
                if key and upload_id:
                    client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
                    aborted += 1

            truncated = str(response.get("IsTruncated", "false")).lower() == "true"
            if not truncated:
                return aborted
            next_key = str(response.get("NextKeyMarker") or "")
            next_upload = str(response.get("NextUploadIdMarker") or "")
            if not next_key or (next_key == key_marker and next_upload == upload_marker):
                raise RuntimeError("COS returned a truncated multipart page without usable markers")
            key_marker, upload_marker = next_key, next_upload

    @staticmethod
    def _delete_batch(client: Any, bucket: str, targets: list[dict[str, str]]) -> int:
        if not targets:
            return 0
        result = client.delete_objects(
            Bucket=bucket,
            Delete={"Quiet": "true", "Object": targets},
        )
        errors = result.get("Error") or []
        if errors:
            error_count = 1 if isinstance(errors, dict) else len(errors)
            raise RuntimeError(f"COS failed to delete {error_count} object(s)")
        return len(targets)

    async def apurge_mount(self, policy: Any, mount: Any) -> int:
        return await asyncio.to_thread(self.purge_mount, policy, mount)

    def list_objects(
        self,
        policy: Any,
        mount: Any,
        *,
        prefix: str = "",
        delimiter: str = "",
        marker: str = "",
        max_keys: int = 1000,
    ) -> StorageObjectPage:
        if not 1 <= max_keys <= 1000:
            raise ValueError("max_keys must be between 1 and 1000")
        response = self._client_for(policy).list_objects(
            Bucket=self._bucket(policy),
            Prefix=self._list_prefix(policy, mount, prefix),
            Delimiter=delimiter,
            Marker=marker,
            MaxKeys=max_keys,
        )
        return page_from_response(self, policy, mount, response)

    async def alist_objects(self, policy: Any, mount: Any, **kwargs: Any) -> StorageObjectPage:
        return await asyncio.to_thread(self.list_objects, policy, mount, **kwargs)

    def iter_objects(self, policy: Any, mount: Any, **kwargs: Any) -> Iterator[StorageObject]:
        marker = str(kwargs.pop("marker", ""))
        while True:
            page = self.list_objects(policy, mount, marker=marker, **kwargs)
            yield from page.objects
            if not page.is_truncated:
                return
            if not page.next_marker or page.next_marker == marker:
                raise RuntimeError("COS returned a truncated page without a usable next marker")
            marker = page.next_marker

    async def aiter_objects(self, policy: Any, mount: Any, **kwargs: Any) -> AsyncIterator[StorageObject]:
        marker = str(kwargs.pop("marker", ""))
        while True:
            page = await self.alist_objects(policy, mount, marker=marker, **kwargs)
            for item in page.objects:
                yield item
            if not page.is_truncated:
                return
            if not page.next_marker or page.next_marker == marker:
                raise RuntimeError("COS returned a truncated page without a usable next marker")
            marker = page.next_marker

    def head_object(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        verify_encryption: bool = False,
    ) -> StorageObject:
        response = self._client_for(policy).head_object(
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, object_key),
        )
        if verify_encryption:
            assert_encryption(policy, response)
        return object_from_head(self, policy, mount, object_key, response)

    async def ahead_object(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        verify_encryption: bool = False,
    ) -> StorageObject:
        return await asyncio.to_thread(
            self.head_object,
            policy,
            mount,
            object_key,
            verify_encryption=verify_encryption,
        )

    def put_object(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        body: bytes | BinaryIO,
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
        no_overwrite: bool = False,
    ) -> StorageObject:
        if no_overwrite:
            self._require_unversioned(policy)
        kwargs: dict[str, Any] = encryption_args(policy)
        if mime_type:
            kwargs["ContentType"] = mime_type
        if metadata:
            kwargs["Metadata"] = metadata_headers(metadata)
        if no_overwrite:
            kwargs.setdefault("Metadata", {})["x-cos-forbid-overwrite"] = "true"
        response = self._client_for(policy).put_object(
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, object_key),
            Body=body,
            **kwargs,
        )
        assert_encryption(policy, response)
        return StorageObject(
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=object_key,
            storage_path=Path(self._key(policy, mount, object_key)),
            size=len(body) if isinstance(body, bytes) else None,
            etag=str(response.get("ETag", "")),
            version_id=response.get("x-cos-version-id"),
            mime_type=mime_type,
            metadata=metadata or {},
        )

    async def aput_object(self, policy: Any, mount: Any, object_key: str, body: bytes | BinaryIO, **kwargs: Any) -> StorageObject:
        return await asyncio.to_thread(self.put_object, policy, mount, object_key, body, **kwargs)

    def put_directory_marker(self, policy: Any, mount: Any, directory: str) -> None:
        self._require_unversioned(policy)
        key = self._validate_physical_key(self._key(policy, mount, directory).rstrip("/") + "/")
        self._client_for(policy).put_object(
            Bucket=self._bucket(policy), Key=key, Body=b"",
            Metadata={"x-cos-forbid-overwrite": "true"}, **encryption_args(policy),
        )

    def head_directory_marker(self, policy: Any, mount: Any, directory: str) -> int:
        key = self._validate_physical_key(self._key(policy, mount, directory).rstrip("/") + "/")
        response = self._client_for(policy).head_object(Bucket=self._bucket(policy), Key=key)
        size = header(response, "content-length")
        if size is None:
            raise RuntimeError("COS directory marker size is unavailable")
        return int(size)

    def delete_directory_marker(self, policy: Any, mount: Any, directory: str) -> None:
        key = self._validate_physical_key(self._key(policy, mount, directory).rstrip("/") + "/")
        self._client_for(policy).delete_object(Bucket=self._bucket(policy), Key=key)

    def _get_object_response(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        byte_range: str | tuple[int, int | None] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, str] = {}
        if isinstance(byte_range, tuple):
            start, end = byte_range
            if start < 0 or (end is not None and end < start):
                raise ValueError("invalid byte range")
            kwargs["Range"] = f"bytes={start}-{'' if end is None else end}"
        elif byte_range:
            if not byte_range.startswith("bytes="):
                raise ValueError("byte_range must use HTTP bytes= syntax")
            kwargs["Range"] = byte_range
        return self._client_for(policy).get_object(
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, object_key),
            **kwargs,
        )

    def open_object(self, policy: Any, mount: Any, object_key: str, *, byte_range: str | tuple[int, int | None] | None = None) -> BinaryIO:
        return CosStream(self._get_object_response(policy, mount, object_key, byte_range=byte_range)["Body"])

    async def aopen_object(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        byte_range: str | tuple[int, int | None] | None = None,
    ) -> AsyncCosStream:
        response = await asyncio.to_thread(
            self._get_object_response,
            policy,
            mount,
            object_key,
            byte_range=byte_range,
        )
        body = response.pop("Body")
        return AsyncCosStream(CosStream(body), response)

    def delete_object(self, policy: Any, mount: Any, object_key: str, *, hard_delete: bool = False) -> bool:
        _ = hard_delete
        self._client_for(policy).delete_object(
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, object_key),
        )
        return True

    async def adelete_object(self, policy: Any, mount: Any, object_key: str, *, hard_delete: bool = False) -> bool:
        return await asyncio.to_thread(self.delete_object, policy, mount, object_key, hard_delete=hard_delete)

    def copy_object(
        self, policy: Any, mount: Any, source_key: str, destination_key: str, *,
        no_overwrite: bool = False,
    ) -> StorageObject:
        if no_overwrite:
            return self._copy_without_overwrite(policy, mount, source_key, destination_key)
        extra = getattr(policy, "extra", None) or {}
        self._client_for(policy).copy(
            Bucket=self._bucket(policy),
            Key=self._key(policy, mount, destination_key),
            CopySource={
                "Bucket": self._bucket(policy),
                "Key": self._key(policy, mount, source_key),
                "Region": self._region(policy),
            },
            PartSize=int(extra.get("copy_part_size_mb", 16)),
            MAXThread=int(extra.get("copy_concurrency", 3)),
            **encryption_args(policy),
        )
        return self.head_object(policy, mount, destination_key, verify_encryption=True)

    def _copy_without_overwrite(self, policy: Any, mount: Any, source_key: str, destination_key: str) -> StorageObject:
        self._require_unversioned(policy)
        client = self._client_for(policy)
        bucket = self._bucket(policy)
        target = self._key(policy, mount, destination_key)
        source_path = self._key(policy, mount, source_key)
        if source_path == target:
            raise ValueError("COS protected copy requires a different destination")
        source = read_copy_snapshot(client, bucket, source_path)
        copy_source = {"Bucket": bucket, "Key": source_path, "Region": self._region(policy)}
        if source.size <= 5 * 1024 * 1024 * 1024:
            attributes = source.write_args()
            # COS copies the metadata headers itself in Copy mode.
            attributes = {key: value for key, value in attributes.items() if key in {
                "StorageClass", "ServerSideEncryption", "SSEKMSKeyId", "SSEKMSContext",
            }}
            client.copy_object(
                Bucket=bucket, Key=target, CopySource=copy_source,
                CopySourceIfMatch=source.etag,
                ACL=source.initial_acl(),
                Metadata={"x-cos-forbid-overwrite": "true"},
                **attributes,
            )
            self._finish_protected_copy(client, bucket, source_path, target, source)
            return self.head_object(policy, mount, destination_key)

        if source.content_md5:
            raise ValueError("COS multipart copy cannot preserve the source Content-MD5")
        part_size = max(16 * 1024 * 1024, ceil(source.size / self.capabilities.max_parts))
        if part_size > self.capabilities.max_part_size:
            raise ValueError("COS source is too large for multipart copy")
        upload = self.create_multipart_upload(
            policy, mount, destination_key, total_size=source.size,
            _copy_snapshot=source,
        )
        parts: list[MultipartPart] = []
        try:
            for index, start in enumerate(range(0, source.size, part_size), start=1):
                end = min(start + part_size, source.size) - 1
                result = client.upload_part_copy(
                    Bucket=bucket, Key=target, PartNumber=index, UploadId=upload.upload_id,
                    CopySource=copy_source, CopySourceRange=f"bytes={start}-{end}",
                    CopySourceIfMatch=source.etag,
                )
                etag = str(result.get("ETag") or "")
                if not etag:
                    raise RuntimeError("COS did not return a copied part ETag")
                parts.append(MultipartPart(part_number=index, etag=etag, size=end - start + 1))
            if read_copy_snapshot(client, bucket, source_path) != source:
                raise RuntimeError("COS source changed during the protected copy")
            result = self.complete_multipart_upload(
                policy, mount, upload, parts, no_overwrite=True, _verify_encryption=False,
            )
            self._finish_protected_copy(client, bucket, source_path, target, source)
            return result
        except BaseException:
            with suppress(Exception):
                self.abort_multipart_upload(policy, mount, upload)
            raise

    @staticmethod
    def _finish_protected_copy(client: Any, bucket: str, source_path: str, target: str, source: CopySnapshot) -> None:
        if source.acl_mode != "default":
            client.put_object_acl(Bucket=bucket, Key=target, AccessControlPolicy=source.acl_policy())
        copied = read_copy_snapshot(client, bucket, target)
        if copied.preserved_attributes() != source.preserved_attributes():
            raise RuntimeError("COS copied object attributes differ from the source")
        if source.crc64 and source.crc64 != copied.crc64:
            raise RuntimeError("COS copied object checksum differs from the source")
        if source.content_md5 and source.content_md5 != copied.content_md5:
            raise RuntimeError("COS copied object Content-MD5 differs from the source")
        if read_copy_snapshot(client, bucket, source_path) != source:
            raise RuntimeError("COS source changed during the protected copy")

    async def acopy_object(
        self, policy: Any, mount: Any, source_key: str, destination_key: str, *,
        no_overwrite: bool = False,
    ) -> StorageObject:
        return await asyncio.to_thread(
            self.copy_object, policy, mount, source_key, destination_key, no_overwrite=no_overwrite,
        )
