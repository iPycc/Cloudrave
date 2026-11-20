"""Read-only preflight for direct COS and OSS uploads."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import HTTPException

from schemas.user_mount import UploadCorsStatus
from services.explorer.path_write import object_path_template
from services.explorer.up_plan import sign_put
from store.key import format_object_key, managed_key


_BROWSER_MANAGED_HEADERS = {"connection", "content-length", "cookie", "host", "origin", "referer", "user-agent"}


async def check_direct_upload_cors(adapter, policy, mount, origin: str) -> UploadCorsStatus:
    try:
        token = uuid4().hex
        template = object_path_template(policy, mount)
        name = "cloudrave-cors-check.bin"
        key = (
            format_object_key(template, directory="", filename=name, blob_id=token)
            if template else managed_key(mount.owner_id, name)
        )
        async with asyncio.timeout(8):
            signed = await sign_put(adapter, policy, mount, key, "application/octet-stream", None,
                                    path_token=token if template else None)
            endpoint = urlsplit(signed.url)
            if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password:
                return "unavailable"
            if f"{endpoint.scheme}://{endpoint.netloc}" == origin:
                return "same_origin"
            url = endpoint._replace(query="", fragment="").geturl()
            single_headers = {name.lower() for name in signed.headers} - _BROWSER_MANAGED_HEADERS
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                multipart = await _check_put(client, url, origin, set(), require_etag=True)
                if multipart != "ready" or not single_headers:
                    return multipart
                return await _check_put(client, url, origin, single_headers)
    except (HTTPException, httpx.HTTPError, RuntimeError, TimeoutError, ValueError):
        return "unavailable"


async def _check_put(client, url, origin, request_headers, *, require_etag=False) -> UploadCorsStatus:
    headers = {"Origin": origin, "Access-Control-Request-Method": "PUT"}
    if request_headers:
        headers["Access-Control-Request-Headers"] = ",".join(sorted(request_headers))
    async with client.stream("OPTIONS", url, headers=headers) as response:
        if response.status_code in {401, 403}:
            return "blocked"
        if not response.is_success:
            return "unavailable"
        allowed_origin = response.headers.get("Access-Control-Allow-Origin")
        methods = _cors_values(response, "Access-Control-Allow-Methods")
        allowed_headers = _cors_values(response, "Access-Control-Allow-Headers")
        if allowed_origin not in {origin, "*"} or methods.isdisjoint({"put", "*"}):
            return "blocked"
        if "*" not in allowed_headers and not request_headers <= allowed_headers:
            return "blocked"
        if require_etag and _cors_values(response, "Access-Control-Expose-Headers").isdisjoint({"etag", "*"}):
            return "missing_etag"
        return "ready"


def _cors_values(response: httpx.Response, name: str) -> set[str]:
    return {value.strip().lower() for value in response.headers.get(name, "").split(",")}
