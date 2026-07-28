"""Stable, index-friendly physical storage identities."""

from __future__ import annotations

from hashlib import sha256


def physical_hash(provider: str, *parts: object) -> str:
    """Hash a canonical provider locator without persisting credentials."""

    value = "\0".join((provider, *(str(part) for part in parts)))
    return sha256(value.encode("utf-8")).hexdigest()
