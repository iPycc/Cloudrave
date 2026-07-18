"""Small streaming helpers shared by storage adapters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


CHUNK_SIZE = 1024 * 1024


def copy_stream(source: BinaryIO, target: BinaryIO, *, chunk_size: int = CHUNK_SIZE) -> int:
    """Copy a binary stream without buffering the complete payload in memory."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    written = 0
    while chunk := source.read(chunk_size):
        if isinstance(chunk, str):
            chunk = chunk.encode()
        target.write(chunk)
        written += len(chunk)
    return written


def write_atomic(target: Path, body: bytes | BinaryIO) -> int:
    """Write one object next to its destination and atomically publish it."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.part")
    try:
        with temporary.open("wb") as output:
            if isinstance(body, bytes):
                output.write(body)
                size = len(body)
            else:
                size = copy_stream(body, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        return size
    finally:
        temporary.unlink(missing_ok=True)
