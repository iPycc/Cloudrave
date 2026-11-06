"""Bound actual 7z output, rather than trusting archive size metadata."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from py7zr.io import Py7zIO, WriterFactory


class BoundedArchiveWriter(Py7zIO):
    def __init__(self, factory, path: Path):
        self.factory = factory
        self.path = path
        self.position = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=False)

    def write(self, data):
        self.factory.check_deadline()
        if self.position + len(data) > self.factory.max_entry or self.factory.written + len(data) > self.factory.max_total:
            raise ValueError("Archive output exceeds extraction limits")
        with self.path.open("r+b") as stream:
            stream.seek(self.position)
            written = stream.write(data)
        self.position += written
        self.factory.written += written
        return written

    def read(self, size=None):
        with self.path.open("rb") as stream:
            stream.seek(self.position)
            data = stream.read(size)
        self.position += len(data)
        return data

    def seek(self, offset, whence=0):
        bases = {0: 0, 1: self.position, 2: self.size()}
        position = bases[whence] + offset
        if position < 0 or position > self.factory.max_entry:
            raise ValueError("Invalid archive output offset")
        self.position = position
        return position

    def flush(self):
        pass

    def size(self):
        return self.path.stat().st_size


class BoundedArchiveFactory(WriterFactory):
    def __init__(self, root: Path, *, max_total: int, max_entry: int, max_entries: int, check_deadline):
        self.root = root.resolve()
        self.max_total = max_total
        self.max_entry = max_entry
        self.max_entries = max_entries
        self.check_deadline = check_deadline
        self.written = 0
        self.entries = 0

    def create(self, filename):
        self.check_deadline()
        parts = PurePosixPath(filename.replace("\\", "/").lstrip("/")).parts
        path = self.root.joinpath(*parts).resolve()
        if not parts or ".." in parts or not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("Invalid archive output path")
        self.entries += 1
        if self.entries > self.max_entries:
            raise ValueError("Too many archive entries")
        return BoundedArchiveWriter(self, path)
