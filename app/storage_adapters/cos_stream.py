"""Async facade over the COS SDK's synchronous streaming response body."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class CosStream:
    """Closable binary facade for qcloud_cos.StreamBody."""

    def __init__(self, body: Any) -> None:
        self._body = body

    def read(self, size: int = -1) -> bytes:
        if size >= 0:
            chunk = self._body.read(size)
            return chunk.encode() if isinstance(chunk, str) else chunk
        chunks: list[bytes] = []
        while chunk := self.read(1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        close = getattr(self._body, "close", None)
        if close is not None:
            close()
            return
        get_raw_stream = getattr(self._body, "get_raw_stream", None)
        raw = get_raw_stream() if get_raw_stream is not None else None
        if raw is not None and getattr(raw, "close", None) is not None:
            raw.close()

    def __iter__(self):
        return iter(self._body)

    def __enter__(self) -> "CosStream":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class AsyncCosStream:
    def __init__(self, body: Any, headers: dict[str, Any] | None = None) -> None:
        self._body = body if isinstance(body, CosStream) else CosStream(body)
        self.headers = headers or {}

    async def read(self, size: int = -1) -> bytes:
        return await asyncio.to_thread(self._body.read, size)

    async def iter_chunks(self, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        while chunk := await self.read(chunk_size):
            yield chunk

    async def close(self) -> None:
        close = getattr(self._body, "close", None)
        if close is not None:
            await asyncio.to_thread(close)

    async def __aenter__(self) -> "AsyncCosStream":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
