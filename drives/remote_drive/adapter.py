"""Read-through, mirror-mode base adapter for remote consumer drives."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

import httpx

from drives.remote_drive.models import RemoteAccount, RemoteEntry
from drives.remote_drive.stream import RemoteHttpStream
from store.identity import physical_hash
from store.port import StorageAdapterBase
from store.types import PresignedURL, StorageCapability, StorageObject, StorageObjectPage, normalize_object_key, utcnow


ClientFactory = Callable[[dict[str, str]], Any]


@dataclass(slots=True)
class _RemoteObject:
    key: str
    entry: RemoteEntry


class RemoteMirrorAdapter(StorageAdapterBase):
    capabilities = StorageCapability(
        supports_presign=False,
        supports_multipart=False,
        supports_cors_autoconfig=False,
        supports_direct_write=False,
        supports_single_put=False,
        supports_range=True,
        supports_mirror=True,
        upload_transport="cloudrave",
        requires_part_etag=False,
        max_object_size=0,
    )

    def __init__(self, provider_type: str, client_factory: ClientFactory, staging_root: Path) -> None:
        self.provider_type = provider_type
        self._client_factory = client_factory
        self._staging_root = staging_root / ".remote-drive"
        self._cache: dict[str, tuple[float, list[_RemoteObject]]] = {}
        self._known: dict[str, _RemoteObject] = {}

    def prepare_mount_root(self, mount) -> Path:  # type: ignore[override]
        root = self._staging_root / self.provider_type / str(mount.id)
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _credentials(policy) -> dict[str, str]:
        return getattr(policy, "credentials", {}) or {}

    def _client(self, policy):
        return self._client_factory(self._credentials(policy))

    def inspect_credentials(self, credentials: dict[str, str]) -> RemoteAccount:
        client = self._client_factory(dict(credentials))
        # Inspection is read-only: rotated secrets must only be produced for
        # a saved policy, where the credential runtime can persist them.
        client.allow_token_refresh = False
        return client.inspect()

    def validate_mount(self, policy, mount) -> dict[str, object]:
        account = self._client(policy).inspect()
        return {
            "account_id": account.account_id,
            "root_id": account.root_id,
            "used_bytes": account.used_bytes,
            "total_bytes": account.total_bytes,
        }

    def _cache_key(self, policy, mount) -> str:
        credentials = self._credentials(policy)
        digest = hashlib.sha256(repr(sorted(credentials.items())).encode()).hexdigest()
        return f"{mount.id}:{digest}"

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = value.replace("/", "／").replace("\\", "＼").strip()
        return cleaned or "未命名文件"

    def _snapshot(self, policy, mount, *, refresh: bool = False) -> list[_RemoteObject]:
        cache_key = self._cache_key(policy, mount)
        cached = self._cache.get(cache_key)
        if not refresh and cached and time.monotonic() - cached[0] < 30:
            return cached[1]

        client = self._client(policy)
        root_id = self._credentials(policy).get("root_id") or getattr(client, "root_id", "")
        queue = deque([(str(root_id), "")])
        objects: list[_RemoteObject] = []
        used_paths: set[str] = set()
        visited: set[str] = set()
        while queue:
            parent_id, parent_path = queue.popleft()
            if parent_id in visited:
                continue
            visited.add(parent_id)
            for entry in client.list_children(parent_id):
                name = self._safe_name(entry.name)
                path = f"{parent_path}/{name}".strip("/")
                if entry.is_dir:
                    queue.append((entry.remote_id, path))
                    continue
                if path in used_paths:
                    stem, dot, suffix = name.rpartition(".")
                    discriminator = entry.remote_id[-8:]
                    name = f"{stem or name} ({discriminator}){dot}{suffix}" if dot else f"{name} ({discriminator})"
                    path = f"{parent_path}/{name}".strip("/")
                used_paths.add(path)
                objects.append(_RemoteObject(normalize_object_key(path), entry))
                if len(objects) > 250_000:
                    raise RuntimeError("运营商云盘文件数量超过单次镜像索引上限（250000）")
        objects.sort(key=lambda item: item.key)
        self._cache[cache_key] = (time.monotonic(), objects)
        return objects

    def _find(self, policy, mount, object_key: str) -> _RemoteObject:
        key = normalize_object_key(object_key)
        known = self._known.get(f"{self._cache_key(policy, mount)}:{key}")
        if known is not None:
            return known
        for item in self._snapshot(policy, mount):
            if item.key == key:
                return item
        for item in self._snapshot(policy, mount, refresh=True):
            if item.key == key:
                return item
        raise FileNotFoundError(key)

    def _remember(self, policy, mount, object_key: str, entry: RemoteEntry) -> _RemoteObject:
        item = _RemoteObject(normalize_object_key(object_key), entry)
        self._known[f"{self._cache_key(policy, mount)}:{item.key}"] = item
        self._cache.pop(self._cache_key(policy, mount), None)
        return item

    def _forget(self, policy, mount, object_key: str) -> None:
        key = normalize_object_key(object_key)
        self._known.pop(f"{self._cache_key(policy, mount)}:{key}", None)
        self._cache.pop(self._cache_key(policy, mount), None)

    def physical_id(self, policy, mount, object_key: str) -> str:
        item = self._find(policy, mount, object_key)
        return physical_hash(self.provider_type, item.entry.remote_id)

    def list_objects(self, policy, mount, *, prefix: str = "", marker: str | None = None, max_keys: int = 1000, delimiter: str | None = None) -> StorageObjectPage:
        normalized_prefix = prefix.replace("\\", "/").lstrip("/")
        items = [item for item in self._snapshot(policy, mount) if item.key.startswith(normalized_prefix) and (not marker or item.key > marker)]
        selected = items[: max(1, min(int(max_keys), 1000))]
        objects = [self._storage_object(mount, item) for item in selected]
        truncated = len(items) > len(selected)
        return StorageObjectPage(objects=objects, next_marker=selected[-1].key if truncated and selected else None, is_truncated=truncated)

    def iter_objects(self, policy, mount, **kwargs) -> Iterator[StorageObject]:
        marker = kwargs.pop("marker", None)
        while True:
            page = self.list_objects(policy, mount, marker=marker, **kwargs)
            yield from page.objects
            if not page.is_truncated:
                return
            marker = page.next_marker

    def _storage_object(self, mount, item: _RemoteObject) -> StorageObject:
        modified = item.entry.modified_at.isoformat() if item.entry.modified_at else ""
        return StorageObject(
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=item.key,
            storage_path=Path(item.entry.remote_id),
            size=item.entry.size,
            etag=hashlib.sha256(f"{item.entry.remote_id}:{item.entry.size}:{modified}".encode()).hexdigest(),
            metadata={"last_modified": modified, "remote_id": item.entry.remote_id},
        )

    def head_object(self, policy, mount, object_key: str) -> StorageObject:
        return self._storage_object(mount, self._find(policy, mount, object_key))

    def build_download_url(self, policy, mount, object_key: str, *, expires_in: int | None = None, download_name: str | None = None, inline: bool = False) -> PresignedURL:
        _ = (download_name, inline)
        item = self._find(policy, mount, object_key)
        remote_id = item.entry.download_id or item.entry.remote_id
        ttl = max(60, int(expires_in or getattr(policy, "presign_ttl_seconds", 900)))
        return PresignedURL(method="GET", url=self._client(policy).download_url(remote_id), expires_at=utcnow() + timedelta(seconds=ttl))

    def open_object(self, policy, mount, object_key: str, *, byte_range: str | tuple[int, int | None] | None = None) -> BinaryIO:
        signed = self.build_download_url(policy, mount, object_key)
        headers: dict[str, str] = {}
        if isinstance(byte_range, str):
            headers["Range"] = byte_range
        elif byte_range:
            start, end = byte_range
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        client = httpx.Client(follow_redirects=True, timeout=60.0)
        try:
            response = client.send(client.build_request("GET", signed.url, headers=headers), stream=True)
            response.raise_for_status()
        except Exception:
            client.close()
            raise
        return RemoteHttpStream(client, response)

    def mount_has_objects(self, policy, mount) -> bool:
        return bool(self.list_objects(policy, mount, max_keys=1).objects)

    def purge_mount(self, policy, mount) -> int:
        _ = policy
        root = self.prepare_mount_root(mount)
        count = sum(1 for path in root.rglob("*") if path.is_file())
        shutil.rmtree(root, ignore_errors=True)
        return count

    def configure_cors(self, policy, mount) -> dict[str, object]:
        return {"provider_type": self.provider_type, "mount_id": mount.id, "status": "noop", "message": "运营商云盘不需要浏览器 CORS 配置"}

    @staticmethod
    def _read_only(*args, **kwargs):
        _ = (args, kwargs)
        raise PermissionError("当前运营商云盘驱动为只读镜像，请在运营商客户端中修改文件")

    put_object = _read_only
    copy_object = _read_only
    delete_object = _read_only
    presign_put = _read_only
    create_multipart_upload = _read_only
    presign_part = _read_only
    upload_part = _read_only
    list_parts = _read_only
    complete_multipart_upload = _read_only
    abort_multipart_upload = _read_only
