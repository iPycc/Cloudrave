"""Portable local mount paths, validated before filesystem resolution."""
from __future__ import annotations

import re


def local_root_parts(value: str) -> tuple[str, ...]:
    raw = value.replace("\\", "/").strip()
    if raw.lower() == "/upload" or raw.lower().startswith("/upload/"):
        raw = raw[len("/upload"):]
    parts = tuple(part for part in raw.strip("/").split("/") if part)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"{prefix}{i}" for prefix in ("COM", "LPT") for i in "123456789¹²³")}
    for part in parts:
        if (part in {".", ".."} or part.endswith((".", " "))
                or any(ord(char) < 32 for char in part)
                or re.search(r'[:<>"|?*]', part)
                or part.split(".", 1)[0].upper() in reserved):
            raise ValueError("Invalid local storage path segment")
    return parts
