"""High-throughput preview response helpers."""

from __future__ import annotations

import stat
from pathlib import Path

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse, Response


PREVIEW_CACHE_CONTROL = "private, max-age=3600"


def _normalize_etag(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    return candidate


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True

    normalized_etag = _normalize_etag(etag)
    return any(
        _normalize_etag(candidate) == normalized_etag
        for candidate in if_none_match.split(",")
    )


def build_local_preview_response(
    request: Request,
    path: Path,
    *,
    filename: str,
    media_type: str,
) -> FileResponse | Response:
    """Serve a local preview with byte ranges and private conditional caching."""

    try:
        stat_result = path.stat()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preview file not found",
        ) from exc

    if not stat.S_ISREG(stat_result.st_mode):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preview file not found",
        )

    response = FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
        stat_result=stat_result,
        headers={"Cache-Control": PREVIEW_CACHE_CONTROL},
    )

    # A range request must reach FileResponse so it can return 206/416. For a
    # normal cached request, avoid opening the file at all when the ETag matches.
    if request.headers.get("range") is None and _etag_matches(
        request.headers.get("if-none-match"),
        response.headers["etag"],
    ):
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": PREVIEW_CACHE_CONTROL,
                "ETag": response.headers["etag"],
                "Last-Modified": response.headers["last-modified"],
            },
        )

    return response
