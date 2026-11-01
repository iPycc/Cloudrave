"""Sized binary reader over the OSS SDK V2 response stream."""

from __future__ import annotations

from typing import Any


class OssStream:
    """Expose ``read(size)`` without buffering the entire remote object."""

    def __init__(self, body: Any, *, chunk_size: int = 1024 * 1024) -> None:
        self._body = body
        self._chunks = iter(body.iter_bytes(chunk_size=chunk_size))
        self._buffer = bytearray()
        self._finished = False

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            chunks = [bytes(self._buffer)] if self._buffer else []
            self._buffer.clear()
            chunks.extend(self._chunks)
            self._finished = True
            return b"".join(chunks)
        while len(self._buffer) < size and not self._finished:
            try:
                self._buffer.extend(next(self._chunks))
            except StopIteration:
                self._finished = True
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def close(self) -> None:
        self._body.close()

    def __enter__(self) -> "OssStream":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
