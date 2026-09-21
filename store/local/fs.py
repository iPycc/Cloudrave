"""Local filesystem storage adapter."""
from __future__ import annotations

import shutil
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4

from store.port import StorageAdapterBase
from store.identity import physical_hash
from store.types import (
    MultipartPart,
    MultipartUploadState,
    PresignedURL,
    StorageCapability,
    StorageObject,
    StorageObjectPage,
    normalize_object_key,
    utcnow,
)
from store.stream import copy_stream, write_atomic


class LocalFsAdapter(StorageAdapterBase):
    provider_type = "local_fs"
    capabilities = StorageCapability(
        supports_presign=False,
        supports_multipart=True,
        supports_cors_autoconfig=False,
        supports_direct_write=True,
        supports_single_put=False,
        supports_mirror=False,
        upload_transport="cloudrave",
        requires_part_etag=False,
    )

    def __init__(self, base_root: str | Path | None = None) -> None:
        self.base_root = Path(base_root or Path.cwd() / "upload" / "local")

    def prepare_mount_root(self, mount) -> Path:  # type: ignore[override]
        mount_root = self._resolve_mount_root(mount)
        self._ensure_directory(mount_root / "objects")
        self._ensure_directory(mount_root / "trash")
        self._ensure_directory(mount_root / ".multipart")
        return mount_root

    def physical_id(self, policy, mount, object_key: str) -> str:
        _ = policy
        path = os.path.normcase(str(self._resolve_object_path(mount, object_key).resolve()))
        return physical_hash(self.provider_type, path)

    def validate_mount(self, policy, mount) -> dict[str, object]:
        _ = policy
        root = self.prepare_mount_root(mount)
        page = self.list_objects(policy, mount, max_keys=1)
        return {
            "root": str(root),
            "sample_count": len(page.objects),
            "has_more": page.is_truncated,
        }

    def _resolve_mount_root(self, mount) -> Path:
        base_root = self.base_root.resolve()
        extra = getattr(mount, "extra", {}) or {}
        configured_root = extra.get("storage_root")
        if isinstance(configured_root, str) and configured_root.strip():
            normalized = configured_root.replace("\\", "/").strip()
            if normalized.lower().startswith("/upload"):
                normalized = normalized[7:]
            normalized = normalized.strip("/")
            candidate = (base_root / normalized).resolve() if normalized else base_root
            return self._contained(base_root, candidate, "storage_root")

        candidate = (base_root / str(mount.mount_slug)).resolve()
        return self._contained(base_root, candidate, "mount_slug")

    def _resolve_object_path(self, mount, object_key: str) -> Path:
        normalized = self._local_key(object_key)
        root = (self.prepare_mount_root(mount) / "objects").resolve()
        return self._contained(root, (root / normalized).resolve(), "object_key")

    def local_path(self, mount, object_key: str) -> Path:
        """Return a validated local path for optimized internal file serving."""

        return self._resolve_object_path(mount, object_key)

    def _resolve_trash_path(self, mount, object_key: str) -> Path:
        normalized = self._local_key(object_key)
        root = (self.prepare_mount_root(mount) / "trash").resolve()
        return self._contained(root, (root / normalized).resolve(), "object_key")

    def _resolve_multipart_dir(self, mount, upload_id: str) -> Path:
        safe_id = self._local_key(upload_id)
        root = (self.prepare_mount_root(mount) / ".multipart").resolve()
        return self._contained(root, (root / safe_id).resolve(), "upload_id")

    @staticmethod
    def _contained(root: Path, candidate: Path, field: str) -> Path:
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"{field} resolves outside the local storage root")
        return candidate

    @staticmethod
    def _local_key(value: str) -> str:
        raw = value.replace("\\", "/").lstrip("/")
        for part in raw.split("/"):
            if part.endswith((".", " ")):
                raise ValueError("object key contains a non-portable local path segment")
        normalized = normalize_object_key(value)
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        for part in normalized.split("/"):
            if ":" in part or any(ord(char) < 32 for char in part) or part.endswith((".", " ")):
                raise ValueError("object key contains a non-portable local path segment")
            if part.split(".", 1)[0].upper() in reserved or re.search(r'[<>"|?*]', part):
                raise ValueError("object key contains an invalid local path segment")
        return normalized

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
        target = self._resolve_object_path(mount, object_key)
        self._ensure_directory(target.parent)
        size = write_atomic(target, body)
        return StorageObject(
            provider_type=self.provider_type,
            mount_id=mount.id,
            object_key=normalize_object_key(object_key),
            storage_path=target,
            size=size,
            etag=str(uuid4()),
            mime_type=mime_type,
            metadata=dict(metadata or {}),
        )

    def list_objects(
        self,
        policy,
        mount,
        *,
        prefix: str = "",
        marker: str | None = None,
        max_keys: int = 1000,
        delimiter: str | None = None,
    ) -> StorageObjectPage:
        _ = policy
        object_root = (self.prepare_mount_root(mount) / "objects").resolve()
        raw_prefix = prefix.replace("\\", "/").lstrip("/")
        normalized_prefix = ""
        if raw_prefix:
            trailing = raw_prefix.endswith("/")
            normalized_prefix = normalize_object_key(raw_prefix.rstrip("/"))
            if trailing:
                normalized_prefix += "/"

        keys = sorted(
            path.relative_to(object_root).as_posix()
            for path in object_root.rglob("*")
            if path.is_file()
        )
        keys = [
            key for key in keys
            if key.startswith(normalized_prefix) and (marker is None or key > marker)
        ]

        objects: list[StorageObject] = []
        common_prefixes: list[str] = []
        seen_prefixes: set[str] = set()
        last_scanned: str | None = None
        limit = max(1, min(int(max_keys), 1000))
        for key in keys:
            last_scanned = key
            remainder = key[len(normalized_prefix):]
            if delimiter and delimiter in remainder:
                common = normalized_prefix + remainder.split(delimiter, 1)[0] + delimiter
                if common not in seen_prefixes:
                    seen_prefixes.add(common)
                    common_prefixes.append(common)
            else:
                objects.append(self.head_object(policy, mount, key))
            if len(objects) + len(common_prefixes) >= limit:
                break

        is_truncated = bool(last_scanned and keys and last_scanned != keys[-1])
        return StorageObjectPage(
            objects=objects,
            common_prefixes=common_prefixes,
            next_marker=last_scanned if is_truncated else None,
            is_truncated=is_truncated,
        )

    def iter_objects(self, policy, mount, **kwargs) -> Iterator[StorageObject]:
        marker = kwargs.pop("marker", None)
        while True:
            page = self.list_objects(policy, mount, marker=marker, **kwargs)
            yield from page.objects
            if not page.is_truncated:
                return
            if not page.next_marker or page.next_marker == marker:
                raise RuntimeError("Local filesystem returned a truncated object page without a marker")
            marker = page.next_marker

    def open_object(self, policy, mount, object_key: str) -> BinaryIO:
        target = self._resolve_object_path(mount, object_key)
        return target.open("rb")

    def head_object(self, policy, mount, object_key: str) -> StorageObject:
        target = self._resolve_object_path(mount, object_key)
        stat = target.stat()
        return StorageObject(
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=normalize_object_key(object_key),
            storage_path=target,
            size=stat.st_size,
            etag=f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"',
        )

    def copy_object(self, policy, mount, source_key: str, destination_key: str) -> StorageObject:
        source = self._resolve_object_path(mount, source_key)
        target = self._resolve_object_path(mount, destination_key)
        self._ensure_directory(target.parent)
        shutil.copy2(source, target)
        return self.head_object(policy, mount, destination_key)

    def delete_object(self, policy, mount, object_key: str, *, hard_delete: bool = False) -> bool:
        source = self._resolve_object_path(mount, object_key)
        if not source.exists():
            return False
        if hard_delete:
            source.unlink()
            return True
        target = self._resolve_trash_path(mount, object_key)
        self._ensure_directory(target.parent)
        shutil.move(str(source), str(target))
        return True

    def mount_has_objects(self, policy, mount) -> bool:
        _ = policy
        mount_root = self._resolve_mount_root(mount)
        if not mount_root.exists():
            return False
        return any(path.is_file() for path in mount_root.rglob("*"))

    def purge_mount(self, policy, mount) -> int:
        _ = policy
        mount_root = self._resolve_mount_root(mount)
        if not mount_root.exists():
            return 0
        deleted = sum(1 for path in mount_root.rglob("*") if path.is_file())
        for directory in ("objects", "trash", ".multipart"):
            target = self._contained(
                mount_root,
                (mount_root / directory).resolve(),
                directory,
            )
            if target.exists():
                shutil.rmtree(target)
        return deleted

    def presign_put(
        self,
        policy,
        mount,
        object_key: str,
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> PresignedURL:
        _ = metadata
        target = self._resolve_object_path(mount, object_key)
        self._ensure_directory(target.parent)
        return PresignedURL(
            method="PUT",
            url=target.as_uri(),
            headers={
                "X-Cloudrave-Local-Write": "1",
                **({"Content-Type": mime_type} if mime_type else {}),
            },
            expires_at=utcnow() + timedelta(seconds=policy.presign_ttl_seconds),
        )

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
        upload_id = uuid4().hex
        upload_dir = self._resolve_multipart_dir(mount, upload_id)
        self._ensure_directory(upload_dir / "parts")
        return MultipartUploadState(
            upload_id=upload_id,
            provider_type=self.provider_type,
            mount_id=mount.id,
            object_key=normalize_object_key(object_key),
            upload_root=self.prepare_mount_root(mount),
            upload_dir=upload_dir,
            part_size_bytes=policy.part_size_mb * 1024 * 1024,
            total_size=total_size,
            metadata=dict(metadata or {}),
            expires_at=utcnow() + timedelta(seconds=policy.presign_ttl_seconds),
        )

    def presign_part(self, policy, mount, upload: MultipartUploadState, part_number: int) -> PresignedURL:
        self._validate_part_number(part_number)
        part_path = upload.upload_dir / "parts" / f"{part_number:06d}.part"
        return PresignedURL(
            method="PUT",
            url=part_path.as_uri(),
            headers={"X-Cloudrave-Local-Part": str(part_number)},
            expires_at=upload.expires_at,
            upload_id=upload.upload_id,
            part_number=part_number,
        )

    def upload_part(
        self,
        policy,
        mount,
        upload: MultipartUploadState,
        part_number: int,
        body: bytes | BinaryIO,
    ) -> MultipartPart:
        self._validate_part_number(part_number)
        part_dir = upload.upload_dir / "parts"
        self._ensure_directory(part_dir)
        part_path = part_dir / f"{part_number:06d}.part"
        size = write_atomic(part_path, body)
        return MultipartPart(
            part_number=part_number,
            etag=str(uuid4()),
            size=size,
            local_path=part_path,
        )

    def list_parts(self, policy, mount, upload: MultipartUploadState) -> list[MultipartPart]:
        part_dir = upload.upload_dir / "parts"
        if not part_dir.exists():
            return []
        parts: list[MultipartPart] = []
        for part_path in sorted(part_dir.glob("*.part")):
            try:
                part_number = int(part_path.stem)
            except ValueError:
                continue
            parts.append(
                MultipartPart(
                    part_number=part_number,
                    etag=hex(part_path.stat().st_mtime_ns),
                    size=part_path.stat().st_size,
                    local_path=part_path,
                )
            )
        return parts

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
        target = self._resolve_object_path(mount, upload.object_key)
        self._ensure_directory(target.parent)
        ordered_parts = sorted(parts, key=lambda item: item.part_number)
        temporary = target.with_name(f".{target.name}.{upload.upload_id}.part")
        try:
            with temporary.open("wb") as output:
                for part in ordered_parts:
                    if part.local_path is None or not part.local_path.exists():
                        raise FileNotFoundError(f"multipart part {part.part_number} is missing")
                    with part.local_path.open("rb") as source:
                        copy_stream(source, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        size = target.stat().st_size
        shutil.rmtree(upload.upload_dir, ignore_errors=True)
        return StorageObject(
            provider_type=self.provider_type,
            mount_id=mount.id,
            object_key=upload.object_key,
            storage_path=target,
            size=size,
            etag=str(uuid4()),
            mime_type=mime_type,
            metadata=dict(metadata or {}),
        )

    def abort_multipart_upload(self, policy, mount, upload: MultipartUploadState) -> bool:
        if not upload.upload_dir.exists():
            return False
        shutil.rmtree(upload.upload_dir, ignore_errors=True)
        return True

    def build_download_url(
        self,
        policy,
        mount,
        object_key: str,
        *,
        expires_in: int | None = None,
        download_name: str | None = None,
        inline: bool = False,
    ) -> PresignedURL:
        _ = (download_name, inline)
        ttl = policy.presign_ttl_seconds if expires_in is None else expires_in
        if ttl <= 0:
            raise ValueError("expires_in must be positive")
        target = self._resolve_object_path(mount, object_key)
        return PresignedURL(
            method="GET",
            url=target.as_uri(),
            headers={},
            expires_at=utcnow() + timedelta(seconds=ttl),
        )

    def configure_cors(self, policy, mount) -> dict[str, object]:
        return {
            "provider_type": self.provider_type,
            "mount_id": mount.id,
            "status": "noop",
            "message": "local filesystem does not require bucket CORS",
        }

    @staticmethod
    def _validate_part_number(part_number: int) -> None:
        if not 1 <= part_number <= 10_000:
            raise ValueError("multipart part_number must be between 1 and 10000")
