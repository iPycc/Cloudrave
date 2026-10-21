"""Writable browser-direct adapter for token-authenticated operator drives."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from threading import Lock
from typing import BinaryIO
from uuid import uuid4

from drives.remote_drive.adapter import ClientFactory, RemoteMirrorAdapter
from drives.remote_drive.models import RemoteEntry
from store.identity import physical_hash
from store.types import (
    MultipartPart,
    MultipartUploadState,
    PresignedURL,
    StorageCapability,
    StorageObject,
    normalize_object_key,
)


class RemoteDirectAdapter(RemoteMirrorAdapter):
    """Translate Cloudrave multipart sessions into each provider's upload protocol.

    Providers may either receive bytes directly from the browser or require
    Cloudrave to relay each part when their upload endpoint does not support
    third-party browser origins.
    """

    def __init__(self, provider_type: str, client_factory: ClientFactory, staging_root: Path) -> None:
        super().__init__(provider_type, client_factory, staging_root)
        upload_transport = getattr(client_factory, "UPLOAD_TRANSPORT", "presigned")
        self._parent_lock = Lock()
        self.capabilities = StorageCapability(
            supports_presign=upload_transport == "presigned",
            supports_multipart=True,
            supports_cors_autoconfig=False,
            supports_direct_write=True,
            supports_single_put=False,
            supports_range=True,
            supports_mirror=True,
            download_transport="browser",
            upload_transport=upload_transport,
            requires_part_etag=False,
            supports_part_listing=False,
            eager_part_plans=bool(getattr(client_factory, "EAGER_PART_PLANS", True)),
            part_checksum_algorithm=getattr(client_factory, "PART_CHECKSUM_ALGORITHM", None),
            part_size_alignment=int(getattr(client_factory, "PART_SIZE_ALIGNMENT", 1)),
            min_part_size=int(getattr(client_factory, "MIN_PART_SIZE", 5 * 1024 * 1024)),
            max_part_size=int(getattr(client_factory, "MAX_PART_SIZE", 16 * 1024 * 1024)),
            max_parts=int(getattr(client_factory, "MAX_PARTS", 10_000)),
            max_object_size=int(getattr(client_factory, "MAX_OBJECT_SIZE", 50 * 1024 * 1024 * 1024)),
        )

    def _ensure_parent(self, policy, object_key: str) -> str:
        # Upload sessions are created concurrently. Serialize parent discovery
        # so two files do not both create the same remote directory.
        with self._parent_lock:
            client = self._client(policy)
            parent_id = self._credentials(policy).get("root_id") or getattr(client, "root_id", "")
            parts = normalize_object_key(object_key).split("/")[:-1]
            for name in parts:
                existing = next(
                    (entry for entry in client.list_children(parent_id) if entry.is_dir and entry.name == name),
                    None,
                )
                if existing is None:
                    existing = client.create_folder(parent_id, name)
                parent_id = existing.remote_id
            return str(parent_id)

    def create_multipart_upload(
        self,
        policy,
        mount,
        object_key: str,
        *,
        total_size: int | None = None,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MultipartUploadState:
        size = int(total_size or 0)
        key = normalize_object_key(object_key)
        parent_id = self._ensure_parent(policy, key)
        client = self._client(policy)
        part_size = int((metadata or {}).get("part_size") or self.capabilities.min_part_size)
        provider_upload_id = client.begin_upload(
            parent_id=parent_id,
            file_name=key.rsplit("/", 1)[-1],
            total_size=size,
            part_size=part_size,
            provider_checksum=(metadata or {}).get("provider_checksum") or None,
            mime_type=mime_type,
        )
        root = self.prepare_mount_root(mount)
        return MultipartUploadState(
            upload_id=provider_upload_id,
            provider_upload_id=provider_upload_id,
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=key,
            upload_root=root,
            upload_dir=root / ".multipart" / hashlib.sha256(provider_upload_id.encode()).hexdigest(),
            part_size_bytes=part_size,
            total_size=size,
            metadata={**(metadata or {}), "parent_id": parent_id},
        )

    def presign_part(self, policy, mount, upload: MultipartUploadState, part_number: int) -> PresignedURL:
        _ = mount
        plan = self._client(policy).part_plan(upload, part_number)
        plan.upload_id = upload.upload_id
        plan.part_number = part_number
        return plan

    def list_parts(self, policy, mount, upload: MultipartUploadState) -> list[MultipartPart]:
        _ = (policy, mount, upload)
        return []

    def complete_multipart_upload(
        self,
        policy,
        mount,
        upload: MultipartUploadState,
        parts: list[MultipartPart],
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StorageObject:
        _ = metadata
        entry = self._client(policy).complete_upload(upload, parts, mime_type=mime_type)
        item = self._remember(policy, mount, upload.object_key, entry)
        return self._storage_object(mount, item)

    def abort_multipart_upload(self, policy, mount, upload: MultipartUploadState) -> bool:
        _ = mount
        abort = getattr(self._client(policy), "abort_upload", None)
        return bool(abort(upload) if abort else True)

    def delete_object(self, policy, mount, object_key: str, *, hard_delete: bool = False) -> bool:
        _ = hard_delete
        item = self._find(policy, mount, object_key)
        self._client(policy).delete_entry(item.entry)
        self._forget(policy, mount, object_key)
        return True

    def configure_cors(self, policy, mount) -> dict[str, object]:
        _ = policy
        if self.capabilities.upload_transport == "cloudrave":
            return {
                "provider_type": self.provider_type,
                "mount_id": mount.id,
                "status": "not-required",
                "message": "上传分片由 Cloudrave 后端转发，不依赖运营商上传域名的浏览器 CORS",
            }
        return {
            "provider_type": self.provider_type,
            "mount_id": mount.id,
            "status": "provider-managed",
            "message": "运营商上传域名的 CORS 由运营商控制，Cloudrave 不会改写或中转请求",
        }

    def put_object(
        self,
        policy,
        mount,
        object_key: str,
        body: bytes | BinaryIO,
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StorageObject:
        """Support Cloudrave's small server-generated objects (for example new text files)."""
        _ = metadata
        key = normalize_object_key(object_key)
        parent_id = self._ensure_parent(policy, key)
        target_name = key.rsplit("/", 1)[-1]
        try:
            replaced = self._find(policy, mount, key)
        except FileNotFoundError:
            replaced = None
        upload_name = target_name if replaced is None else f".cloudrave-{uuid4().hex}-{target_name}"
        with tempfile.NamedTemporaryFile(prefix="cloudrave-drive-", suffix=".upload", delete=True) as temporary:
            if isinstance(body, bytes):
                temporary.write(body)
            else:
                while chunk := body.read(1024 * 1024):
                    temporary.write(chunk)
            temporary.flush()
            entry = self._client(policy).upload_file(
                parent_id,
                Path(temporary.name),
                file_name=upload_name,
                mime_type=mime_type,
            )
        if replaced is not None:
            client = self._client(policy)
            client.delete_entry(replaced.entry)
            client.rename_entry(entry, target_name)
            entry = RemoteEntry(
                entry.remote_id,
                entry.parent_id,
                target_name,
                entry.is_dir,
                entry.size,
                entry.modified_at,
                entry.download_id,
            )
        return self._storage_object(mount, self._remember(policy, mount, key, entry))

    @staticmethod
    def copy_object(*args, **kwargs):
        _ = (args, kwargs)
        raise NotImplementedError("运营商云盘的物理对象复制不允许退回 Cloudrave 中转")

    @staticmethod
    def presign_put(*args, **kwargs):
        _ = (args, kwargs)
        raise NotImplementedError("运营商云盘使用直连分片上传")

    def upload_part(
        self,
        policy,
        mount,
        upload: MultipartUploadState,
        part_number: int,
        body: bytes | BinaryIO,
    ) -> MultipartPart:
        _ = mount
        client = self._client(policy)
        relay = getattr(client, "upload_part", None)
        if not callable(relay):
            raise NotImplementedError("当前运营商云盘不支持由 Cloudrave 转发上传分片")
        return relay(upload, part_number, body)

    def physical_id(self, policy, mount, object_key: str) -> str:
        return physical_hash(self.provider_type, self._find(policy, mount, object_key).entry.remote_id)

    @staticmethod
    def validate_completed_object(policy, mount, stored_object: StorageObject, checksum: str | None = None) -> None:
        _ = (policy, mount, checksum)
        if stored_object.size is None or stored_object.size < 0:
            raise RuntimeError("运营商云盘未返回有效的上传结果")
