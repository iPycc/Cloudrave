"""Shared helpers for remote drive protocol clients."""

from __future__ import annotations

import html
import base64
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


class RemoteProtocolError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class RemoteAuthenticationError(RemoteProtocolError):
    pass


def require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RemoteProtocolError(f"{context} returned an invalid response")
    return value


def find_number(value: Any, names: tuple[str, ...]) -> int | None:
    """Find a numeric field in a shallow or nested JSON response."""

    if isinstance(value, Mapping):
        for name in names:
            candidate = value.get(name)
            if candidate not in (None, ""):
                try:
                    return int(float(str(candidate)))
                except (TypeError, ValueError):
                    pass
        for candidate in value.values():
            found = find_number(candidate, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = find_number(candidate, names)
            if found is not None:
                return found
    return None


def parse_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y%m%d%H%M%S",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def clean_url(value: object) -> str:
    return html.unescape(str(value or "").strip()).replace("http://", "https://", 1)


def encode_upload_state(value: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_upload_state(value: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteProtocolError("上传会话状态无效") from exc
    if not isinstance(decoded, dict):
        raise RemoteProtocolError("上传会话状态无效")
    return decoded
