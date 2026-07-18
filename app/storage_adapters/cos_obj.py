"""COS bucket and ordinary object operations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, BinaryIO

from app.storage.types import StorageObject, StorageObjectPage
from app.storage_adapters.cos_parse import object_from_head, page_from_response
from app.storage_adapters.cos_sec import assert_encryption, encryption_args, metadata_headers
from app.storage_adapters.cos_stream import AsyncCosStream, CosStream


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
    ) -> StorageObject:
        kwargs: dict[str, Any] = encryption_args(policy)
        if mime_type:
            kwargs["ContentType"] = mime_type
        if metadata:
            kwargs["Metadata"] = metadata_headers(metadata)
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

    def copy_object(self, policy: Any, mount: Any, source_key: str, destination_key: str) -> StorageObject:
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

    async def acopy_object(self, policy: Any, mount: Any, source_key: str, destination_key: str) -> StorageObject:
        return await asyncio.to_thread(self.copy_object, policy, mount, source_key, destination_key)
