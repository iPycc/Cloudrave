"""Portable virtual-file name validation."""

from __future__ import annotations

import re


INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def valid_name(value: str) -> str:
    name = value.strip()
    if not name or name in {".", ".."} or name.endswith((".", " ")):
        raise ValueError("Invalid file name")
    if INVALID_CHARS.search(name):
        raise ValueError("File name contains unsupported characters")
    if name.split(".", 1)[0].upper() in RESERVED:
        raise ValueError("File name is reserved by the local filesystem")
    if len(name) > 255:
        raise ValueError("File name is too long")
    return name
