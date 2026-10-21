"""Streaming wrapper for provider download responses."""

from __future__ import annotations

from collections.abc import Iterator

import httpx


class RemoteHttpStream:
    """Expose an httpx streaming response through the storage read contract."""

    def __init__(self, client: httpx.Client, response: httpx.Response) -> None:
        self._client = client
        self._response = response
        self._chunks: Iterator[bytes] = response.iter_bytes(chunk_size=1024 * 1024)
        self._buffer = bytearray()
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        if size is None or size < 0:
            payload = bytes(self._buffer) + b"".join(self._chunks)
            self._buffer.clear()
            return payload
        if size == 0:
            return b""
        while len(self._buffer) < size:
            try:
                self._buffer.extend(next(self._chunks))
            except StopIteration:
                break
        payload = bytes(self._buffer[:size])
        del self._buffer[:size]
        return payload

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._client.close()

    def __enter__(self) -> "RemoteHttpStream":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
