"""Range-aware proxy for private remote objects used by online previews."""

from __future__ import annotations

import asyncio
import inspect
import mimetypes
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from app.services.explorer.source import content_disposition
from app.storage.call import call_adapter
from app.storage.errors import is_not_found


CHUNK_SIZE = 1024 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range; multipart ranges are intentionally rejected."""

    if value is None:
        return None
    match = RANGE_RE.fullmatch(value.strip())
    if match is None or size <= 0:
        raise ValueError("invalid range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("invalid range")
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid range")
        return max(size - suffix, 0), size - 1
    start = int(start_text)
    if start >= size:
        raise ValueError("range starts beyond object")
    end = int(end_text) if end_text else size - 1
    if end < start:
        raise ValueError("invalid range")
    return start, min(end, size - 1)


def _etag(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith('W/"') or value.startswith('"'):
        return value
    return f'"{value.strip(chr(34))}"'


def _etag_matches(header: str | None, etag: str | None) -> bool:
    if not header or not etag:
        return False
    values = {item.strip() for item in header.split(",")}
    if "*" in values:
        return True
    expected = etag.removeprefix("W/")
    return any(value.removeprefix("W/") == expected for value in values)


def _if_range_matches(header: str | None, etag: str | None) -> bool:
    if not header or not etag:
        return False
    candidate = header.strip()
    if candidate.startswith("W/") or etag.startswith("W/"):
        return False
    return candidate == etag


async def _chunks(stream: Any) -> AsyncIterator[bytes]:
    try:
        iterator = getattr(stream, "iter_chunks", None)
        if iterator is not None:
            async for chunk in iterator(CHUNK_SIZE):
                if chunk:
                    yield chunk
            return
        while True:
            chunk = await asyncio.to_thread(stream.read, CHUNK_SIZE)
            if not chunk:
                return
            yield chunk
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            if inspect.iscoroutinefunction(close):
                await close()
            else:
                await asyncio.to_thread(close)


async def remote_response(
    request: Request,
    *,
    adapter: Any,
    policy: Any,
    mount: Any,
    object_key: str,
    filename: str,
    inline: bool = True,
    cache_control: str = "private, no-store",
) -> Response:
    """Stream a private provider object without buffering it in ASGI memory."""

    try:
        obj = await call_adapter(adapter, "head_object", policy, mount, object_key)
    except Exception as exc:
        if is_not_found(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="预览源文件在存储中不存在",
            ) from exc
        raise
    size = int(obj.size or 0)
    etag = _etag(obj.etag)
    media_type = obj.mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
        "Content-Disposition": content_disposition(filename, inline=inline),
        "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
    }
    if etag:
        base_headers["ETag"] = etag
    if request.headers.get("range") is None and _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=base_headers)

    range_value = request.headers.get("range")
    if range_value and (if_range := request.headers.get("if-range")) is not None and not _if_range_matches(if_range, etag):
        range_value = None
    try:
        byte_range = parse_range(range_value, size)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="Requested range is not satisfiable",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        ) from exc

    response_status = status.HTTP_200_OK
    open_range: str | None = None
    if byte_range is None:
        base_headers["Content-Length"] = str(size)
    else:
        start, end = byte_range
        response_status = status.HTTP_206_PARTIAL_CONTENT
        open_range = f"bytes={start}-{end}"
        base_headers["Content-Length"] = str(end - start + 1)
        base_headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    if request.method == "HEAD":
        return Response(status_code=response_status, media_type=media_type, headers=base_headers)
    try:
        stream = await call_adapter(
            adapter,
            "open_object",
            policy,
            mount,
            object_key,
            **({"byte_range": open_range} if open_range else {}),
        )
    except Exception as exc:
        if is_not_found(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="预览源文件在存储中不存在",
            ) from exc
        raise
    return StreamingResponse(
        _chunks(stream),
        status_code=response_status,
        media_type=media_type,
        headers=base_headers,
    )
