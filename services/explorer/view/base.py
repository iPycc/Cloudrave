"""Shared preview constants, cache locks, and HTTP helpers."""

from __future__ import annotations

import asyncio
import hashlib
import stat
import weakref
from datetime import UTC
from pathlib import Path

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from starlette.datastructures import URL

from models.tables import Node


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

SEEKABLE_MAX_BYTES = 512 * 1024 * 1024
OFFICE_CARD_MAX_BYTES = 96 * 1024 * 1024

_asset_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_asset_locks_guard = asyncio.Lock()
derivative_semaphore = asyncio.Semaphore(4)


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
    updated = (
        node.updated_at.astimezone(UTC).isoformat()
        if node.updated_at.tzinfo
        else node.updated_at.replace(tzinfo=UTC).isoformat()
    )
    return hashlib.sha256(f"{node.id}:{node.size}:{updated}".encode()).hexdigest()[:20]


def normalize_etag(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    return candidate.strip('"')


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    normalized_etag = normalize_etag(etag)
    return any(normalize_etag(candidate) == normalized_etag for candidate in if_none_match.split(","))


def relative_preview_url(url: URL) -> str:
    """Keep authenticated preview traffic on the frontend's same-origin /api proxy."""

    return f"{url.path}?{url.query}" if url.query else url.path


async def lock_for(key: str) -> asyncio.Lock:
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
    if request.headers.get("range") is None and etag_matches(request.headers.get("if-none-match"), response.headers["etag"]):
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
