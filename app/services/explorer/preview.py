"""Preview manifests, derived assets, and high-throughput file responses."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
from io import BytesIO
import json
import math
import mimetypes
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
import weakref
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from mutagen import File as MutagenFile
from mutagen import MutagenError
from starlette.datastructures import URL

from app.models.enums import StoragePolicyType
from app.models.tables import BucketMount, Node, StoragePolicy, User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.schemas.preview import (
    OfficeCardPreviewRead,
    PreviewAssetRead,
    PreviewManifestRead,
    PreviewPrepareRead,
    TextContentUpdateRead,
)
from app.security.jwt import AuthError, TokenService
from app.services.explorer.download import DownloadFileService
from app.storage.manager import StorageManager
from app.storage.call import call_adapter
from app.storage.key import managed_key
from app.storage_adapters.local_fs import LocalFsAdapter


PREVIEW_CACHE_CONTROL = "private, max-age=3600"
DERIVED_CACHE_CONTROL = "private, max-age=31536000, immutable"
OFFICE_SOURCE_PURPOSE = "office-source"
IMAGE_VARIANTS = {"thumbnail": 320, "thumbnail_2x": 640, "screen": 1280, "screen_2x": 1920}
OFFICE_EXTENSIONS = {
    "doc", "docx", "docm", "dot", "dotx",
    "xls", "xlsx", "xlsm", "xlsb", "xltx",
    "ppt", "pptx", "pptm", "pps", "ppsx",
}
TEXT_EXTENSIONS = {
    "txt", "md", "json", "log", "csv", "xml", "yaml", "yml", "toml", "ini", "conf",
    "ts", "tsx", "js", "jsx", "py", "go", "rs", "java", "kt", "php", "rb", "c", "cpp",
    "h", "hpp", "css", "scss", "less", "html", "sql", "sh", "ps1", "bat",
}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "ico", "heic", "tif", "tiff"}
VIDEO_EXTENSIONS = {"mp4", "mov", "mkv", "avi", "webm", "flv", "m4v", "wmv"}
AUDIO_EXTENSIONS = {"mp3", "wav", "flac", "ogg", "m4a", "aac", "opus"}
ARCHIVE_EXTENSIONS = {"zip", "tar", "gz", "bz2", "xz"}

_asset_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_asset_locks_guard = asyncio.Lock()
_media_jobs: dict[str, tuple[str, str | None]] = {}
_media_tasks: set[asyncio.Task[None]] = set()
_derivative_semaphore = asyncio.Semaphore(4)
_media_job_semaphore = asyncio.Semaphore(2)
MEDIA_MARKER_STALE_SECONDS = 120
SEEKABLE_MAX_BYTES = 512 * 1024 * 1024
OFFICE_CARD_MAX_BYTES = 96 * 1024 * 1024
OFFICE_XML_MAX_BYTES = 8 * 1024 * 1024
OFFICE_IMAGE_MAX_BYTES = 24 * 1024 * 1024

_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def preview_kind(name: str) -> str:
    extension = Path(name).suffix.lower().lstrip(".")
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension == "pdf":
        return "pdf"
    if extension in OFFICE_EXTENSIONS:
        return "office"
    if extension in TEXT_EXTENSIONS:
        return "text"
    if extension in ARCHIVE_EXTENSIONS:
        return "archive"
    return "unsupported"


def preview_version(node: Node) -> str:
    updated = node.updated_at.astimezone(UTC).isoformat() if node.updated_at.tzinfo else node.updated_at.replace(tzinfo=UTC).isoformat()
    return hashlib.sha256(f"{node.id}:{node.size}:{updated}".encode()).hexdigest()[:20]


def _normalize_etag(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    return candidate.strip('"')


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    normalized_etag = _normalize_etag(etag)
    return any(_normalize_etag(candidate) == normalized_etag for candidate in if_none_match.split(","))


def relative_preview_url(url: URL) -> str:
    """Keep authenticated preview traffic on the frontend's same-origin /api proxy."""

    return f"{url.path}?{url.query}" if url.query else url.path


async def _lock_for(key: str) -> asyncio.Lock:
    async with _asset_locks_guard:
        return _asset_locks.setdefault(key, asyncio.Lock())


def build_local_preview_response(
    request: Request,
    path: Path,
    *,
    filename: str,
    media_type: str,
    cache_control: str = PREVIEW_CACHE_CONTROL,
) -> FileResponse | Response:
    """Serve a local asset with byte ranges and private conditional caching."""

    try:
        stat_result = path.stat()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview file not found") from exc
    if not stat.S_ISREG(stat_result.st_mode):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview file not found")

    response = FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
        stat_result=stat_result,
        headers={
            "Cache-Control": cache_control,
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )
    if request.headers.get("range") is None and _etag_matches(request.headers.get("if-none-match"), response.headers["etag"]):
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": cache_control,
                "ETag": response.headers["etag"],
                "Last-Modified": response.headers["last-modified"],
                "X-Content-Type-Options": "nosniff",
            },
        )
    return response


class PreviewService:
    """Resolve files once, then expose a renderer-oriented preview contract."""

    def __init__(
        self,
        *,
        resolver: DownloadFileService,
        nodes: NodeRepository,
        objects: ObjectRepository,
        audits: AuditLogRepository,
        storage_manager: StorageManager,
        token_service: TokenService,
        cache_root: Path,
        public_base_url: str,
        office_ttl_seconds: int,
        text_max_bytes: int,
    ) -> None:
        self.resolver = resolver
        self.nodes = nodes
        self.objects = objects
        self.audits = audits
        self.storage_manager = storage_manager
        self.token_service = token_service
        self.cache_root = cache_root
        self.public_base_url = public_base_url
        self.office_ttl_seconds = office_ttl_seconds
        self.text_max_bytes = text_max_bytes

    async def manifest(self, request: Request, actor: User, node_id: int) -> PreviewManifestRead:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        manifest = await self._build_manifest(request, node, mount, policy)
        if Path(node.name).suffix.lower() == ".zip" and manifest.status == "ready":
            manifest.capabilities.append("extract")
        return manifest

    async def shared_manifest(
        self,
        request: Request,
        node_id: int,
        *,
        source_url: str,
        archive_entry_url: str | None = None,
        cover_url: str | None = None,
    ) -> PreviewManifestRead:
        node, mount, policy = await self.resolver.resolve_shared(node_id)
        manifest = await self._build_manifest(request, node, mount, policy)
        assets = {
            "source": manifest.assets["source"].model_copy(update={"url": source_url}),
        }
        if manifest.kind == "office":
            for name in ("office_source", "office_viewer"):
                if name in manifest.assets:
                    assets[name] = manifest.assets[name]
        if cover_url is not None and "cover" in manifest.assets:
            assets["cover"] = manifest.assets["cover"].model_copy(update={"url": cover_url})
        if manifest.kind == "archive" and archive_entry_url is not None:
            assets["archive_entry"] = PreviewAssetRead(
                url=archive_entry_url,
                mime_type="application/octet-stream",
            )
        return manifest.model_copy(
            update={
                "assets": assets,
                "capabilities": [
                    capability
                    for capability in manifest.capabilities
                    if capability not in {"edit", "extract"}
                ],
            }
        )

    async def _build_manifest(
        self,
        request: Request,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
    ) -> PreviewManifestRead:
        record = await self.objects.get_by_node_id(node.id)
        version = preview_version(node)
        kind = preview_kind(node.name)
        mime_type = (record.content_type if record else None) or mimetypes.guess_type(node.name)[0] or "application/octet-stream"
        source_url = relative_preview_url(
            request.url_for("preview_file", node_id=node.id).include_query_params(v=version)
        )
        assets: dict[str, PreviewAssetRead] = {
            "source": PreviewAssetRead(
                url=source_url,
                mime_type=mime_type,
                size=node.size,
                supports_range=True,
            )
        }
        metadata: dict[str, object] = {"extension": Path(node.name).suffix.lower().lstrip(".")}
        capabilities = ["download", "properties"]
        manifest_status: str = "ready"
        error: str | None = None
        requires_preparation = False
        preparation_available = shutil.which("ffmpeg") is not None

        if kind == "image":
            capabilities.extend(["zoom", "pan", "rotate", "actual-size"])
            try:
                image_metadata = await self._cached_json(
                    node,
                    "image-metadata",
                    lambda: self._probe_image(node, mount, policy),
                )
                metadata.update(image_metadata)
                for variant, width in IMAGE_VARIANTS.items():
                    assets[variant] = PreviewAssetRead(
                        url=relative_preview_url(
                            request.url_for("preview_image_asset", node_id=node.id, variant=variant).include_query_params(v=version)
                        ),
                        mime_type="image/webp",
                        width=min(width, int(image_metadata["width"])),
                    )
                if max(int(image_metadata["width"]), int(image_metadata["height"])) > 4096:
                    assets["dzi"] = PreviewAssetRead(
                        url=relative_preview_url(request.url_for("preview_dzi", node_id=node.id).include_query_params(v=version)),
                        mime_type="application/xml",
                    )
                    metadata["tiled"] = True
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                metadata["probe_error"] = "Image metadata unavailable"
                error = str(exc)
                if metadata["extension"] == "svg":
                    error = None
        elif kind in {"video", "audio"}:
            capabilities.extend(["play", "seek"])
            media_dir = self._media_dir(node, version)
            media_metadata = await self._cached_json(
                node,
                "audio-metadata-v2" if kind == "audio" else "media-metadata",
                lambda: self._probe_audio(node, mount, policy, media_dir / "cover.webp")
                if kind == "audio"
                else self._probe_media(node, mount, policy),
            )
            metadata.update(media_metadata)
            job_key = self._job_key(node, version)
            job_state, job_error = _media_jobs.get(job_key, ("idle", None))
            processing_marker = media_dir / ".processing"
            marker_active = False
            if processing_marker.exists():
                try:
                    marker_active = datetime.now(UTC).timestamp() - processing_marker.stat().st_mtime < MEDIA_MARKER_STALE_SECONDS
                except FileNotFoundError:
                    marker_active = False
                if not marker_active:
                    processing_marker.unlink(missing_ok=True)
            if kind == "video" and (media_dir / "master.m3u8").exists():
                assets["hls"] = PreviewAssetRead(
                    url=relative_preview_url(
                        request.url_for("preview_media_asset", node_id=node.id, version=version, asset_path="master.m3u8")
                    ),
                    mime_type="application/vnd.apple.mpegurl",
                    supports_range=True,
                )
            elif kind == "audio" and (media_dir / "audio.mp3").exists():
                assets["audio"] = PreviewAssetRead(
                    url=relative_preview_url(
                        request.url_for("preview_media_asset", node_id=node.id, version=version, asset_path="audio.mp3")
                    ),
                    mime_type="audio/mpeg",
                    supports_range=True,
                )
            else:
                requires_preparation = not self._browser_compatible(kind, node.name, media_metadata)
            if kind == "audio" and (media_dir / "cover.webp").exists():
                assets["cover"] = PreviewAssetRead(
                    url=relative_preview_url(
                        request.url_for(
                            "preview_media_asset",
                            node_id=node.id,
                            version=version,
                            asset_path="cover.webp",
                        )
                    ),
                    mime_type="image/webp",
                    supports_range=True,
                )
            if kind == "video" and preparation_available:
                assets["poster"] = PreviewAssetRead(
                    url=relative_preview_url(
                        request.url_for("preview_video_poster", node_id=node.id).include_query_params(v=version)
                    ),
                    mime_type="image/webp",
                )
            if requires_preparation and not preparation_available:
                manifest_status = "unsupported"
                error = "FFmpeg is required to preview this media format"
            if job_state == "processing" or marker_active:
                manifest_status = "processing"
            elif job_state == "failed":
                manifest_status = "failed"
                error = job_error or "Media preparation failed"
        elif kind == "pdf":
            capabilities.extend(["pages", "search", "zoom", "print"])
        elif kind == "office":
            capabilities.append("microsoft-viewer")
            office_source = self._office_source_url(request, node, mount, policy, version)
            assets["office_source"] = PreviewAssetRead(url=office_source, mime_type=mime_type, size=node.size)
            assets["office_viewer"] = PreviewAssetRead(
                url=f"https://view.officeapps.live.com/op/embed.aspx?src={quote(office_source, safe='')}",
                mime_type="text/html",
            )
            metadata["external_service"] = "Microsoft Office Web Viewer"
            metadata["requires_public_https"] = True
            metadata["public_source_ready"] = office_source.startswith("https://")
        elif kind == "text":
            capabilities.append("syntax")
            editable = node.size <= self.text_max_bytes
            if editable:
                capabilities.append("edit")
            metadata["editable"] = editable
            metadata["max_bytes"] = self.text_max_bytes
        elif kind == "archive":
            capabilities.append("list-entries")
            try:
                entries = await self._cached_json(
                    node,
                    "archive-entries",
                    lambda: self._archive_entries(node, mount, policy),
                )
                metadata["entries"] = entries
                metadata["entry_count"] = len(entries)
                metadata["entries_truncated"] = len(entries) == 200
            except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
                manifest_status = "unsupported"
                error = str(exc)
        else:
            manifest_status = "unsupported"
            error = "No online renderer is available for this file type"

        return PreviewManifestRead(
            node_id=node.id,
            name=node.name,
            version=version,
            kind=kind,
            status=manifest_status,
            mime_type=mime_type,
            size=node.size,
            metadata=metadata,
            assets=assets,
            capabilities=capabilities,
            requires_preparation=requires_preparation,
            preparation_available=preparation_available,
            error=error,
        )

    async def image_asset(self, actor: User, node_id: int, variant: str) -> tuple[Path, str]:
        if variant not in IMAGE_VARIANTS:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown image preview variant")
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "image":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not an image")
        version = preview_version(node)
        target = self.cache_root / "images" / str(node.id) / version / f"{variant}.webp"
        if not target.exists():
            lock = await _lock_for(str(target))
            async with lock:
                if not target.exists():
                    async with _derivative_semaphore:
                        await asyncio.to_thread(self._generate_image_variant, node, mount, policy, target, IMAGE_VARIANTS[variant])
        return target, f"{Path(node.name).stem}-{variant}.webp"

    async def office_card_preview(self, actor: User, node_id: int) -> OfficeCardPreviewRead:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "office":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not an Office document")
        if node.size > OFFICE_CARD_MAX_BYTES:
            return OfficeCardPreviewRead(kind="unsupported")

        cover_target = self._office_dir(node, preview_version(node)) / "cover.webp"
        payload = await self._cached_json(
            node,
            "office-card-v2",
            lambda: self._extract_office_card(node, mount, policy, cover_target),
        )
        payload["cover_available"] = cover_target.is_file()
        if not payload["cover_available"]:
            payload["cover_kind"] = None
        return OfficeCardPreviewRead.model_validate(payload)

    async def office_card_cover(self, actor: User, node_id: int) -> Path | None:
        node, _mount, _policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "office":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not an Office document")
        preview = await self.office_card_preview(actor, node_id)
        if not preview.cover_available:
            return None
        target = self._office_dir(node, preview_version(node)) / "cover.webp"
        return target if target.is_file() else None

    async def archive_entry(self, actor: User, node_id: int, entry_path: str) -> tuple[bytes, str, str]:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        return await self._archive_entry(node, mount, policy, entry_path)

    async def shared_archive_entry(self, node_id: int, entry_path: str) -> tuple[bytes, str, str]:
        node, mount, policy = await self.resolver.resolve_shared(node_id)
        return await self._archive_entry(node, mount, policy, entry_path)

    async def _archive_entry(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        entry_path: str,
    ) -> tuple[bytes, str, str]:
        if preview_kind(node.name) != "archive":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not an archive")
        normalized = entry_path.replace("\\", "/").lstrip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid archive entry path")
        try:
            payload = await asyncio.to_thread(self._read_archive_entry, node, mount, policy, normalized)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archive entry not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
        return payload, Path(normalized).name, mimetypes.guess_type(normalized)[0] or "application/octet-stream"

    async def dzi_descriptor(self, actor: User, node_id: int) -> str:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        metadata = await self._cached_json(
            node,
            "image-metadata",
            lambda: self._probe_image(node, mount, policy),
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Image TileSize="256" Overlap="1" Format="webp" xmlns="http://schemas.microsoft.com/deepzoom/2008">'
            f'<Size Width="{metadata["width"]}" Height="{metadata["height"]}"/>'
            '</Image>'
        )

    async def dzi_tile(self, actor: User, node_id: int, level: int, tile_name: str) -> Path:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if not tile_name.endswith(".webp") or "_" not in tile_name:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid image tile")
        try:
            column_text, row_text = tile_name.removesuffix(".webp").split("_", 1)
            column, row = int(column_text), int(row_text)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid image tile") from exc
        if min(level, column, row) < 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid image tile")
        version = preview_version(node)
        target = self.cache_root / "images" / str(node.id) / version / "dzi_files" / str(level) / tile_name
        if not target.exists():
            lock = await _lock_for(str(target))
            async with lock:
                if not target.exists():
                    async with _derivative_semaphore:
                        await asyncio.to_thread(self._generate_dzi_tile, node, mount, policy, target, level, column, row)
        return target

    async def video_poster(self, actor: User, node_id: int) -> Path:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "video":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not a video")
        if shutil.which("ffmpeg") is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="FFmpeg is not installed on the server")
        target = self._media_dir(node, preview_version(node)) / "poster.webp"
        if not target.exists():
            lock = await _lock_for(str(target))
            async with lock:
                if not target.exists():
                    async with _derivative_semaphore:
                        await asyncio.to_thread(self._generate_video_poster, node, mount, policy, target)
        return target

    async def audio_cover(self, actor: User, node_id: int) -> Path | None:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if preview_kind(node.name) != "audio":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not audio")
        version = preview_version(node)
        cover_target = self._media_dir(node, version) / "cover.webp"
        if cover_target.exists():
            return cover_target
        lock = await _lock_for(str(cover_target))
        async with lock:
            if cover_target.exists():
                return cover_target
            try:
                metadata = await asyncio.to_thread(self._probe_audio, node, mount, policy, cover_target)
            except (MutagenError, OSError, ValueError, TypeError, UnidentifiedImageError, Image.DecompressionBombError):
                return None
            if metadata.get("cover_available") and cover_target.exists():
                return cover_target
        return None

    async def shared_audio_cover(self, node_id: int) -> Path | None:
        node, mount, policy = await self.resolver.resolve_shared(node_id)
        if preview_kind(node.name) != "audio":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not audio")
        version = preview_version(node)
        cover_target = self._media_dir(node, version) / "cover.webp"
        if cover_target.exists():
            return cover_target
        lock = await _lock_for(str(cover_target))
        async with lock:
            if cover_target.exists():
                return cover_target
            try:
                metadata = await asyncio.to_thread(self._probe_audio, node, mount, policy, cover_target)
            except (MutagenError, OSError, ValueError, TypeError, UnidentifiedImageError, Image.DecompressionBombError):
                return None
            if metadata.get("cover_available") and cover_target.exists():
                return cover_target
        return None

    async def prepare_media(self, actor: User, node_id: int) -> PreviewPrepareRead:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        kind = preview_kind(node.name)
        if kind not in {"video", "audio"}:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not audio or video")
        if shutil.which("ffmpeg") is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="FFmpeg is not installed on the server")
        version = preview_version(node)
        job_key = self._job_key(node, version)
        state, message = _media_jobs.get(job_key, ("idle", None))
        if state == "processing":
            return PreviewPrepareRead(status="processing", message="Media preparation is already running")
        media_dir = self._media_dir(node, version)
        if (kind == "video" and (media_dir / "master.m3u8").exists()) or (kind == "audio" and (media_dir / "audio.mp3").exists()):
            return PreviewPrepareRead(status="ready", message="Media preview is ready")
        media_dir.mkdir(parents=True, exist_ok=True)
        processing_marker = media_dir / ".processing"
        try:
            marker_fd = os.open(processing_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                marker_age = datetime.now(UTC).timestamp() - processing_marker.stat().st_mtime
            except FileNotFoundError:
                marker_age = MEDIA_MARKER_STALE_SECONDS
            if marker_age < MEDIA_MARKER_STALE_SECONDS:
                return PreviewPrepareRead(status="processing", message="Media preparation is already running")
            processing_marker.unlink(missing_ok=True)
            try:
                marker_fd = os.open(processing_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return PreviewPrepareRead(status="processing", message="Media preparation is already running")
        with os.fdopen(marker_fd, "w", encoding="utf-8") as marker:
            marker.write(f"pid={os.getpid()}\nstarted={datetime.now(UTC).isoformat()}\n")
        _media_jobs[job_key] = ("processing", None)
        task = asyncio.create_task(self._transcode_media(job_key, kind, node, mount, policy, media_dir, processing_marker))
        _media_tasks.add(task)
        task.add_done_callback(_media_tasks.discard)
        return PreviewPrepareRead(status="processing", message=message or "Media preparation started")

    async def media_asset(self, actor: User, node_id: int, version: str, asset_path: str) -> Path:
        node, _mount, _policy = await self.resolver.resolve(actor, node_id)
        if version != preview_version(node):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media preview version is no longer current")
        root = self._media_dir(node, version).resolve()
        target = (root / asset_path).resolve()
        if target != root and root not in target.parents:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid media asset")
        if not target.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
        return target

    async def save_text(
        self,
        actor: User,
        node_id: int,
        *,
        content: str,
        if_match: str | None,
    ) -> TextContentUpdateRead:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        if mount.read_only:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bucket mount is read-only")
        if preview_kind(node.name) != "text":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not editable text")
        locked_node = await self.nodes.get_by_id_for_update(node.id)
        if locked_node is None or locked_node.deleted_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        node = locked_node
        current_version = preview_version(node)
        if not if_match:
            raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="If-Match is required when editing text")
        if _normalize_etag(if_match) != current_version:
            raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail="File changed since it was opened")
        payload = content.encode("utf-8")
        if len(payload) > self.text_max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Text file exceeds the online editing limit")
        record = await self.objects.get_by_node_id(node.id)
        content_type = (record.content_type if record else None) or "text/plain; charset=utf-8"
        adapter = self.storage_manager.get_adapter(policy)
        if not node.blob_path:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="File has no storage object")
        current_blob = node.blob_path
        current_physical = (record.physical_id if record else None) or adapter.physical_id(
            policy,
            mount,
            current_blob,
        )
        references = await self.objects.count_physical_references(
            physical_id=current_physical,
            policy_id=policy.id,
            mount_id=mount.id,
            blob_path=current_blob,
        )
        target_blob = (
            managed_key(
                actor.id,
                node.name,
                style=str((mount.extra or {}).get("object_key_style", "readable")),
            )
            if references > 1
            else current_blob
        )
        extra = dict(record.extra if record else {})
        if target_blob != current_blob:
            extra["origin"] = "managed_edit"
        await call_adapter(
            adapter,
            "put_object",
            policy,
            mount,
            target_blob,
            payload,
            mime_type=content_type,
            metadata=extra,
        )
        checksum = hashlib.sha256(payload).hexdigest()
        node.blob_path = target_blob
        await self.nodes.update_file_metadata(node, size=len(payload))
        await self.objects.create_or_update(
            node_id=node.id,
            policy_id=policy.id,
            blob_path=target_blob,
            physical_id=adapter.physical_id(policy, mount, target_blob),
            revive_physical=True,
            checksum=checksum,
            content_type=content_type,
            extra=extra,
        )
        await self.audits.create(
            actor_id=actor.id,
            action="file.edit",
            target=f"node:{node.id}",
            detail=f"version={preview_version(node)}",
            ip=None,
            user_agent=None,
            request_id=None,
        )
        return TextContentUpdateRead(version=preview_version(node), size=len(payload))

    def validate_office_token(self, token: str, node: Node) -> None:
        try:
            payload = self.token_service.parse_scoped_token(token, OFFICE_SOURCE_PURPOSE)
        except AuthError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid office source token") from exc
        if (
            str(payload.get("sub", "")) != str(node.owner_id)
            or int(payload.get("node_id", 0)) != node.id
            or payload.get("version") != preview_version(node)
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Office source token no longer matches the file")

    def _office_source_url(
        self,
        request: Request,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        version: str,
    ) -> str:
        adapter = self.storage_manager.get_adapter(policy)
        if policy.provider != StoragePolicyType.LOCAL_FS:
            return adapter.build_download_url(policy, mount, node.blob_path, expires_in=self.office_ttl_seconds).url
        token = self.token_service.issue_scoped_token(
            sub=str(node.owner_id),
            purpose=OFFICE_SOURCE_PURPOSE,
            ttl_seconds=self.office_ttl_seconds,
            claims={"node_id": node.id, "version": version},
        )
        source_url = request.url_for("office_source", node_id=node.id).include_query_params(token=token)
        if self.public_base_url:
            return f"{self.public_base_url}{source_url.path}?{source_url.query}"
        return str(source_url)

    def _source_locator(self, node: Node, mount: BucketMount, policy: StoragePolicy, *, expires_in: int = 3600) -> str:
        adapter = self.storage_manager.get_adapter(policy)
        if policy.provider == StoragePolicyType.LOCAL_FS and isinstance(adapter, LocalFsAdapter):
            return str(adapter.resolve_object_path(mount, node.blob_path))
        return adapter.build_download_url(policy, mount, node.blob_path, expires_in=expires_in).url

    def _open_seekable(self, node: Node, mount: BucketMount, policy: StoragePolicy) -> BinaryIO:
        if node.size > SEEKABLE_MAX_BYTES:
            raise OSError("File is too large for seekable server-side preview metadata")
        adapter = self.storage_manager.get_adapter(policy)
        stream = adapter.open_object(policy, mount, node.blob_path)
        if getattr(stream, "seekable", lambda: False)():
            return stream
        spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
        try:
            while chunk := stream.read(1024 * 1024):
                spool.write(chunk)
        finally:
            stream.close()
        spool.seek(0)
        return spool

    def _extract_office_card(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        cover_target: Path,
    ) -> dict[str, object]:
        extension = Path(node.name).suffix.lower().lstrip(".")
        supported = {
            "docx": "document",
            "docm": "document",
            "dotx": "document",
            "xlsx": "spreadsheet",
            "xlsm": "spreadsheet",
            "xltx": "spreadsheet",
            "pptx": "presentation",
            "pptm": "presentation",
            "ppsx": "presentation",
        }
        kind = supported.get(extension)
        if kind is None:
            return {"kind": "unsupported"}

        try:
            with self._open_seekable(node, mount, policy) as stream, zipfile.ZipFile(stream) as archive:
                if kind == "document":
                    return self._word_card(archive)
                if kind == "spreadsheet":
                    return self._spreadsheet_card(archive)
                return self._presentation_card(archive, cover_target)
        except (
            ET.ParseError,
            IndexError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            zipfile.BadZipFile,
        ):
            cover_target.unlink(missing_ok=True)
            return {"kind": "unsupported"}

    @staticmethod
    def _zip_part(archive: zipfile.ZipFile, name: str, *, max_bytes: int = OFFICE_XML_MAX_BYTES) -> bytes:
        normalized = name.replace("\\", "/").lstrip("/")
        info = archive.getinfo(normalized)
        if info.file_size > max_bytes:
            raise ValueError(f"Office preview part is too large: {normalized}")
        payload = archive.read(info)
        if b"<!DOCTYPE" in payload[:4096].upper() or b"<!ENTITY" in payload[:4096].upper():
            raise ValueError("Office preview XML contains a disallowed document type")
        return payload

    @classmethod
    def _xml_part(cls, archive: zipfile.ZipFile, name: str) -> ET.Element:
        return ET.fromstring(cls._zip_part(archive, name))

    @staticmethod
    def _relationship_part(source_part: str) -> str:
        directory, filename = posixpath.split(source_part)
        return posixpath.join(directory, "_rels", f"{filename}.rels")

    @staticmethod
    def _resolve_part(source_part: str, target: str) -> str:
        if target.startswith("/"):
            resolved = posixpath.normpath(target.lstrip("/"))
        else:
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
        if resolved == ".." or resolved.startswith("../"):
            raise ValueError("Office relationship leaves the package root")
        return resolved

    @classmethod
    def _relationships(cls, archive: zipfile.ZipFile, source_part: str) -> dict[str, str]:
        root = cls._xml_part(archive, cls._relationship_part(source_part))
        relationships: dict[str, str] = {}
        for relationship in root.findall(f"{{{_REL_NS}}}Relationship"):
            relationship_id = relationship.get("Id")
            target = relationship.get("Target")
            if not relationship_id or not target or relationship.get("TargetMode") == "External":
                continue
            relationships[relationship_id] = cls._resolve_part(source_part, target)
        return relationships

    @staticmethod
    def _clean_preview_text(value: str, *, limit: int = 240) -> str:
        return re.sub(r"\s+", " ", value).strip()[:limit]

    @classmethod
    def _word_card(cls, archive: zipfile.ZipFile) -> dict[str, object]:
        root = cls._xml_part(archive, "word/document.xml")
        namespace = {"w": _WORD_NS}
        body = root.find("w:body", namespace)
        lines: list[str] = []
        if body is None:
            return {"kind": "document", "lines": lines}

        for block in body:
            if block.tag == f"{{{_WORD_NS}}}p":
                value = cls._clean_preview_text("".join(text.text or "" for text in block.findall(".//w:t", namespace)))
                if value:
                    lines.append(value)
            elif block.tag == f"{{{_WORD_NS}}}tbl":
                for row in block.findall("w:tr", namespace):
                    cells = [
                        cls._clean_preview_text("".join(text.text or "" for text in cell.findall(".//w:t", namespace)), limit=80)
                        for cell in row.findall("w:tc", namespace)
                    ]
                    value = "  |  ".join(cell for cell in cells if cell)
                    if value:
                        lines.append(value)
            if len(lines) >= 40:
                break
        return {"kind": "document", "lines": lines}

    @classmethod
    def _spreadsheet_card(cls, archive: zipfile.ZipFile) -> dict[str, object]:
        namespace = {"x": _SHEET_NS, "r": _DOC_REL_NS}
        workbook_part = "xl/workbook.xml"
        workbook = cls._xml_part(archive, workbook_part)
        relationships = cls._relationships(archive, workbook_part)
        sheet_part: str | None = None
        for sheet in workbook.findall(".//x:sheets/x:sheet", namespace):
            if sheet.get("state", "visible") == "hidden":
                continue
            relationship_id = sheet.get(f"{{{_DOC_REL_NS}}}id")
            if relationship_id and relationship_id in relationships:
                sheet_part = relationships[relationship_id]
                break
        if sheet_part is None:
            return {"kind": "spreadsheet", "rows": []}

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = cls._xml_part(archive, "xl/sharedStrings.xml")
            for item in shared_root.findall("x:si", namespace):
                shared_strings.append(cls._clean_preview_text("".join(text.text or "" for text in item.findall(".//x:t", namespace))))

        sheet_root = cls._xml_part(archive, sheet_part)
        values: dict[tuple[int, int], str] = {}
        for cell in sheet_root.findall(".//x:sheetData/x:row/x:c", namespace):
            reference = cell.get("r", "")
            match = re.match(r"^([A-Z]+)(\d+)$", reference)
            if match is None:
                continue
            column = 0
            for letter in match.group(1):
                column = column * 26 + ord(letter) - 64
            row = int(match.group(2))
            cell_type = cell.get("t")
            raw = cell.findtext("x:v", default="", namespaces=namespace)
            if cell_type == "s" and raw:
                index = int(raw)
                value = shared_strings[index] if 0 <= index < len(shared_strings) else ""
            elif cell_type == "inlineStr":
                value = "".join(text.text or "" for text in cell.findall(".//x:is/x:t", namespace))
            elif cell_type == "b":
                value = "TRUE" if raw == "1" else "FALSE"
            else:
                value = raw
            value = cls._clean_preview_text(value, limit=80)
            if value:
                values[(row, column)] = value
            if len(values) >= 400:
                break

        if not values:
            return {"kind": "spreadsheet", "rows": []}
        first_row = min(row for row, _column in values)
        first_column = min(column for _row, column in values)
        last_row = min(max(row for row, _column in values), first_row + 11)
        last_column = min(max(column for _row, column in values), first_column + 7)
        rows = [
            [values.get((row, column), "") for column in range(first_column, last_column + 1)]
            for row in range(first_row, last_row + 1)
        ]
        while rows and not any(rows[-1]):
            rows.pop()
        while rows and rows[0] and all(not row[-1] for row in rows):
            for row in rows:
                row.pop()
        return {"kind": "spreadsheet", "rows": rows}

    @classmethod
    def _presentation_card(cls, archive: zipfile.ZipFile, cover_target: Path) -> dict[str, object]:
        namespace = {"p": _PRESENTATION_NS, "a": _DRAWING_NS, "r": _DOC_REL_NS}
        presentation_part = "ppt/presentation.xml"
        presentation = cls._xml_part(archive, presentation_part)
        relationships = cls._relationships(archive, presentation_part)
        slide_id = presentation.find(".//p:sldIdLst/p:sldId", namespace)
        relationship_id = slide_id.get(f"{{{_DOC_REL_NS}}}id") if slide_id is not None else None
        slide_part = relationships.get(relationship_id or "", "ppt/slides/slide1.xml")
        slide = cls._xml_part(archive, slide_part)

        title_lines: list[str] = []
        body_lines: list[str] = []
        for shape in slide.findall(".//p:sp", namespace):
            paragraphs = [
                cls._clean_preview_text("".join(text.text or "" for text in paragraph.findall(".//a:t", namespace)))
                for paragraph in shape.findall(".//a:p", namespace)
            ]
            paragraphs = [line for line in paragraphs if line]
            placeholder = shape.find("./p:nvSpPr/p:nvPr/p:ph", namespace)
            placeholder_type = placeholder.get("type", "") if placeholder is not None else ""
            target = title_lines if placeholder_type in {"title", "ctrTitle"} else body_lines
            target.extend(paragraphs)
        lines = (title_lines + body_lines)[:20]

        aspect_ratio: float | None = None
        slide_size = presentation.find("./p:sldSz", namespace)
        if slide_size is not None:
            width = int(slide_size.get("cx", "0") or 0)
            height = int(slide_size.get("cy", "0") or 0)
            if width > 0 and height > 0:
                aspect_ratio = round(width / height, 4)

        cover_kind = cls._write_presentation_cover(archive, slide_part, slide, cover_target)
        return {
            "kind": "presentation",
            "lines": lines,
            "cover_available": cover_kind is not None,
            "cover_kind": cover_kind,
            "aspect_ratio": aspect_ratio,
        }

    @classmethod
    def _write_presentation_cover(
        cls,
        archive: zipfile.ZipFile,
        slide_part: str,
        slide: ET.Element,
        target: Path,
    ) -> str | None:
        names = {name.lower(): name for name in archive.namelist()}
        thumbnail_name = next(
            (
                names[name]
                for name in ("docprops/thumbnail.jpeg", "docprops/thumbnail.jpg", "docprops/thumbnail.png")
                if name in names
            ),
            None,
        )
        candidates: list[tuple[str, str]] = []
        if thumbnail_name is not None:
            candidates.append(("thumbnail", thumbnail_name))
        else:
            try:
                relationships = cls._relationships(archive, slide_part)
            except (KeyError, ET.ParseError, ValueError):
                relationships = {}
            image_parts = []
            for blip in slide.findall(f".//{{{_DRAWING_NS}}}blip"):
                relationship_id = blip.get(f"{{{_DOC_REL_NS}}}embed")
                part = relationships.get(relationship_id or "")
                if part and part not in image_parts:
                    image_parts.append(part)
            candidates.extend(("slide-image", part) for part in image_parts)

        best: tuple[int, str, Image.Image] | None = None
        for cover_kind, part in candidates:
            try:
                payload = cls._zip_part(archive, part, max_bytes=OFFICE_IMAGE_MAX_BYTES)
                with Image.open(BytesIO(payload)) as source:
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    image.load()
                area = image.width * image.height
                if best is None or area > best[0] or cover_kind == "thumbnail":
                    best = (area, cover_kind, image)
                if cover_kind == "thumbnail":
                    break
            except (KeyError, OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError):
                continue
        if best is None:
            target.unlink(missing_ok=True)
            return None

        _area, cover_kind, image = best
        image.thumbnail((960, 540), Image.Resampling.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", suffix=".webp", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            image.save(temporary, "WEBP", quality=86, method=6)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return cover_kind

    async def _cached_json(self, node: Node, category: str, loader: Callable[[], Any]) -> Any:
        target = self.cache_root / "metadata" / str(node.id) / preview_version(node) / f"{category}.json"
        try:
            return await asyncio.to_thread(self._read_json, target)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        lock = await _lock_for(str(target))
        async with lock:
            try:
                return await asyncio.to_thread(self._read_json, target)
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                value = await asyncio.to_thread(loader)
                await asyncio.to_thread(self._write_json_atomic, target, value)
                return value

    @staticmethod
    def _read_json(path: Path) -> Any:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _write_json_atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            temporary = Path(handle.name)
        temporary.replace(path)

    def _probe_image(self, node: Node, mount: BucketMount, policy: StoragePolicy) -> dict[str, object]:
        with self._open_seekable(node, mount, policy) as stream:
            with Image.open(stream) as image:
                oriented = ImageOps.exif_transpose(image)
                return {
                    "width": oriented.width,
                    "height": oriented.height,
                    "format": image.format or "",
                    "mode": image.mode,
                    "frames": int(getattr(image, "n_frames", 1)),
                }

    def _generate_image_variant(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        target: Path,
        width: int,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._open_seekable(node, mount, policy) as stream:
            with Image.open(stream) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((width, width * 4), Image.Resampling.LANCZOS)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                with tempfile.NamedTemporaryFile(suffix=".webp", dir=target.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                try:
                    image.save(temporary, format="WEBP", quality=82, method=4)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)

    def _generate_dzi_tile(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        target: Path,
        level: int,
        column: int,
        row: int,
    ) -> None:
        tile_size, overlap = 256, 1
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._open_seekable(node, mount, policy) as stream:
            with Image.open(stream) as source:
                image = ImageOps.exif_transpose(source)
                max_level = math.ceil(math.log2(max(image.width, image.height)))
                if level > max_level:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image tile level is out of range")
                scale = 2 ** (max_level - level)
                scaled_width = math.ceil(image.width / scale)
                scaled_height = math.ceil(image.height / scale)
                left = max(column * tile_size - (overlap if column else 0), 0)
                top = max(row * tile_size - (overlap if row else 0), 0)
                right = min((column + 1) * tile_size + overlap, scaled_width)
                bottom = min((row + 1) * tile_size + overlap, scaled_height)
                if left >= right or top >= bottom:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image tile is out of range")
                original_box = (
                    min(left * scale, image.width),
                    min(top * scale, image.height),
                    min(right * scale, image.width),
                    min(bottom * scale, image.height),
                )
                tile = image.crop(original_box)
                tile.thumbnail((right - left, bottom - top), Image.Resampling.LANCZOS)
                if tile.mode not in {"RGB", "RGBA"}:
                    tile = tile.convert("RGB")
                with tempfile.NamedTemporaryFile(suffix=".webp", dir=target.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                try:
                    tile.save(temporary, format="WEBP", quality=84, method=4)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)

    def _probe_media(self, node: Node, mount: BucketMount, policy: StoragePolicy) -> dict[str, object]:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return {"probe_available": False}
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", self._source_locator(node, mount, policy)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=20,
            )
            if not result.stdout:
                return {"probe_available": True, "probe_failed": True}
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict):
                return {"probe_available": True, "probe_failed": True}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError):
            return {"probe_available": True, "probe_failed": True}
        metadata: dict[str, object] = {"probe_available": True}
        format_data = payload.get("format", {})
        if format_data.get("duration"):
            metadata["duration"] = round(float(format_data["duration"]), 3)
        if format_data.get("format_name"):
            metadata["container"] = format_data["format_name"]
        for stream in payload.get("streams", []):
            if stream.get("codec_type") == "video" and "video_codec" not in metadata:
                metadata["video_codec"] = stream.get("codec_name", "")
                metadata["width"] = int(stream.get("width", 0))
                metadata["height"] = int(stream.get("height", 0))
            if stream.get("codec_type") == "audio" and "audio_codec" not in metadata:
                metadata["audio_codec"] = stream.get("codec_name", "")
                metadata["sample_rate"] = int(stream.get("sample_rate", 0) or 0)
                metadata["channels"] = int(stream.get("channels", 0) or 0)
        return metadata

    @staticmethod
    def _easy_tag(tags: Any, key: str) -> str:
        if not tags:
            return ""
        value = tags.get(key)
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        return str(value).strip()

    @staticmethod
    def _embedded_cover(audio: Any) -> bytes | None:
        pictures = getattr(audio, "pictures", None)
        if pictures:
            data = getattr(pictures[0], "data", None)
            if isinstance(data, bytes):
                return data
        tags = getattr(audio, "tags", None)
        if tags is None:
            return None
        getall = getattr(tags, "getall", None)
        if callable(getall):
            frames = getall("APIC")
            if frames:
                data = getattr(frames[0], "data", None)
                if isinstance(data, bytes):
                    return data
        covers = tags.get("covr") if hasattr(tags, "get") else None
        if covers:
            candidate = covers[0] if isinstance(covers, (list, tuple)) else covers
            try:
                return bytes(candidate)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _write_audio_cover(payload: bytes, target: Path) -> dict[str, object]:
        if not payload or len(payload) > 20 * 1024 * 1024:
            return {}
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(BytesIO(payload)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            with tempfile.NamedTemporaryFile(suffix=".webp", dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
            try:
                image.save(temporary, format="WEBP", quality=84, method=4)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return {"cover_available": True, "cover_width": image.width, "cover_height": image.height}

    def _probe_audio(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        cover_target: Path,
    ) -> dict[str, object]:
        metadata = self._probe_media(node, mount, policy)
        try:
            with self._open_seekable(node, mount, policy) as stream:
                audio = MutagenFile(stream, easy=True)
                if audio is None:
                    return metadata
                tags = getattr(audio, "tags", None)
                for target_key, tag_key in (
                    ("title", "title"),
                    ("artist", "artist"),
                    ("album", "album"),
                    ("album_artist", "albumartist"),
                    ("date", "date"),
                    ("track", "tracknumber"),
                    ("disc", "discnumber"),
                    ("genre", "genre"),
                ):
                    value = self._easy_tag(tags, tag_key)
                    if value:
                        metadata[target_key] = value
                info = getattr(audio, "info", None)
                if info is not None:
                    for key in ("length", "bitrate", "sample_rate", "channels"):
                        value = getattr(info, key, None)
                        if value is None:
                            continue
                        target_key = "duration" if key == "length" else key
                        metadata[target_key] = round(float(value), 3) if key == "length" else int(value)
                stream.seek(0)
                raw_audio = MutagenFile(stream, easy=False)
                cover = self._embedded_cover(raw_audio)
                if cover:
                    metadata.update(self._write_audio_cover(cover, cover_target))
                else:
                    metadata["cover_available"] = False
        except (MutagenError, OSError, ValueError, TypeError, UnidentifiedImageError, Image.DecompressionBombError):
            metadata["tags_available"] = False
        else:
            metadata["tags_available"] = True
        return metadata

    def _generate_video_poster(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        target: Path,
    ) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise OSError("FFmpeg is not installed")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".webp", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    self._source_locator(node, mount, policy, expires_in=3600),
                    "-vf",
                    "thumbnail=100,scale='min(1280,iw)':-2",
                    "-frames:v",
                    "1",
                    "-an",
                    "-c:v",
                    "libwebp",
                    "-q:v",
                    "75",
                    str(temporary),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=120,
            )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def _browser_compatible(self, kind: str, name: str, metadata: dict[str, object]) -> bool:
        extension = Path(name).suffix.lower().lstrip(".")
        if kind == "video":
            codec = str(metadata.get("video_codec", ""))
            audio_codec = str(metadata.get("audio_codec", ""))
            if extension in {"mp4", "m4v"}:
                return (not codec or codec == "h264") and (not audio_codec or audio_codec in {"aac", "mp3"})
            if extension == "webm":
                return (not codec or codec in {"vp8", "vp9"}) and (not audio_codec or audio_codec in {"opus", "vorbis"})
            return False
        return extension in {"mp3", "m4a", "aac", "wav", "ogg", "opus"}

    async def _transcode_media(
        self,
        job_key: str,
        kind: str,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        media_dir: Path,
        processing_marker: Path,
    ) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            _media_jobs[job_key] = ("failed", "FFmpeg is not installed")
            processing_marker.unlink(missing_ok=True)
            return
        work_dir = media_dir / "_transcode"
        heartbeat = asyncio.create_task(self._heartbeat_marker(processing_marker))
        try:
            async with _media_job_semaphore:
                try:
                    await asyncio.to_thread(shutil.rmtree, work_dir, True)
                    work_dir.mkdir(parents=True, exist_ok=True)
                    source = self._source_locator(node, mount, policy, expires_in=7200)
                    if kind == "video":
                        command = [
                            ffmpeg, "-y", "-i", source,
                            "-map", "0:v:0", "-map", "0:a:0?",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                            "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "128k",
                            "-hls_time", "6", "-hls_playlist_type", "vod",
                            "-hls_segment_filename", str(work_dir / "segment-%05d.ts"),
                            str(work_dir / "master.m3u8"),
                        ]
                    else:
                        command = [ffmpeg, "-y", "-i", source, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(work_dir / "audio.mp3")]
                    await asyncio.to_thread(
                        subprocess.run,
                        command,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=True,
                        timeout=7200,
                    )
                    for asset in work_dir.iterdir():
                        asset.replace(media_dir / asset.name)
                    _media_jobs[job_key] = ("ready", None)
                except (OSError, subprocess.SubprocessError) as exc:
                    _media_jobs[job_key] = ("failed", str(exc))
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await asyncio.to_thread(shutil.rmtree, work_dir, True)
            processing_marker.unlink(missing_ok=True)

    @staticmethod
    async def _heartbeat_marker(marker: Path) -> None:
        while True:
            marker.touch(exist_ok=True)
            await asyncio.sleep(30)

    def _archive_entries(self, node: Node, mount: BucketMount, policy: StoragePolicy) -> list[dict[str, object]]:
        extension = Path(node.name).suffix.lower().lstrip(".")
        if extension not in ARCHIVE_EXTENSIONS:
            raise OSError("This archive format requires downloading the original file")
        entries: list[dict[str, object]] = []
        with self._open_seekable(node, mount, policy) as stream:
            if extension == "zip":
                with zipfile.ZipFile(stream) as archive:
                    for item in archive.infolist()[:200]:
                        entries.append({"name": item.filename, "size": item.file_size, "directory": item.is_dir()})
            else:
                with tarfile.open(fileobj=stream, mode="r:*") as archive:
                    for index, item in enumerate(archive):
                        if index >= 200:
                            break
                        entries.append({"name": item.name, "size": item.size, "directory": item.isdir()})
        return entries

    def _read_archive_entry(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        entry_path: str,
    ) -> bytes:
        max_entry_bytes = 32 * 1024 * 1024
        extension = Path(node.name).suffix.lower().lstrip(".")
        with self._open_seekable(node, mount, policy) as stream:
            if extension == "zip":
                with zipfile.ZipFile(stream) as archive:
                    info = archive.getinfo(entry_path)
                    if info.is_dir():
                        raise KeyError(entry_path)
                    if info.file_size > max_entry_bytes:
                        raise ValueError("Archive entry is too large for online preview")
                    if info.compress_size and info.file_size / info.compress_size > 200:
                        raise ValueError("Archive entry compression ratio is unsafe")
                    return archive.read(info)
            with tarfile.open(fileobj=stream, mode="r:*") as archive:
                info = archive.getmember(entry_path)
                if not info.isfile():
                    raise KeyError(entry_path)
                if info.size > max_entry_bytes:
                    raise ValueError("Archive entry is too large for online preview")
                extracted = archive.extractfile(info)
                if extracted is None:
                    raise KeyError(entry_path)
                return extracted.read(max_entry_bytes + 1)

    def _media_dir(self, node: Node, version: str) -> Path:
        return self.cache_root / "media" / str(node.id) / version

    def _office_dir(self, node: Node, version: str) -> Path:
        return self.cache_root / "office" / str(node.id) / version

    @staticmethod
    def _job_key(node: Node, version: str) -> str:
        return f"{node.id}:{version}"
