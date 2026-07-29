"""Tencent COS upload headers shared by direct and server-side writes."""

from __future__ import annotations

import re
import mimetypes
import unicodedata
from typing import Any
from pathlib import PurePath
from urllib.parse import quote


_META_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


def encryption_args(policy: Any) -> dict[str, str]:
    extra = getattr(policy, "extra", None) or {}
    mode = str(extra.get("server_side_encryption", "none")).strip().lower()
    if mode in {"", "none"}:
        return {}
    if mode == "sse-cos":
        return {"ServerSideEncryption": "AES256"}
    if mode == "sse-kms":
        result = {"ServerSideEncryption": "cos/kms"}
        key_id = str(extra.get("kms_key_id", "")).strip()
        if key_id:
            result["SSEKMSKeyId"] = key_id
        return result
    raise ValueError("server_side_encryption must be none, SSE-COS, or SSE-KMS")


def signed_encryption_headers(policy: Any) -> dict[str, str]:
    args = encryption_args(policy)
    headers: dict[str, str] = {}
    if value := args.get("ServerSideEncryption"):
        headers["x-cos-server-side-encryption"] = value
    if value := args.get("SSEKMSKeyId"):
        headers["x-cos-server-side-encryption-cos-kms-key-id"] = value
    return headers


def metadata_headers(values: dict[str, str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_value in (values or {}).items():
        name = str(raw_name).strip().lower()
        value = str(raw_value)
        if not name or not _META_NAME.fullmatch(name):
            raise ValueError(f"invalid COS metadata name: {raw_name!r}")
        if "\r" in value or "\n" in value:
            raise ValueError("COS metadata values cannot contain newlines")
        result[f"x-cos-meta-{name}"] = value
    return result


def assert_encryption(policy: Any, response: dict[str, Any]) -> None:
    expected_headers = signed_encryption_headers(policy)
    if not expected_headers:
        return
    actual_headers = {str(key).lower(): str(value) for key, value in response.items()}
    for name, expected in expected_headers.items():
        actual = actual_headers.get(name)
        if actual != expected:
            label = " key" if name.endswith("-key-id") else ""
            raise RuntimeError(
                f"COS object encryption{label} mismatch: expected {expected!r}, received {actual!r}"
            )


def response_params(download_name: str | None, inline: bool) -> dict[str, str]:
    disposition = "inline" if inline else "attachment"
    if download_name:
        if "\r" in download_name or "\n" in download_name:
            raise ValueError("download_name cannot contain newlines")
        name = PurePath(download_name.replace("\\", "/")).name
        if not name:
            raise ValueError("download_name must contain a file name")
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", ascii_name).strip() or "download"
        ascii_name = ascii_name.replace('"', "_")
        disposition += f"; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
    params = {"response-content-disposition": disposition}
    if download_name and (mime_type := mimetypes.guess_type(download_name)[0]):
        params["response-content-type"] = mime_type
    return params
