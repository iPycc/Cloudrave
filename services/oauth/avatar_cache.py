"""Download and serve OAuth provider avatars from a local cache.

Providers (Google, GitHub) return avatar URLs on external CDNs that are often
rate limited (e.g. HTTP 429) or blocked for networks without a proxy, so the
browser cannot reach them. This service downloads the provider avatar once
after the link/login exchange and serves it from the Cloudrave origin so the
avatar is always visible.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi.responses import FileResponse

from core.cfg import CloudraveConfig


ALLOWED_AVATAR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _extension_for(content_type: str | None, url: str) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    ext = mapping.get((content_type or "").lower())
    if ext:
        return ext
    suffix = Path(urlsplit(url or "").path).suffix.lower()
    return suffix if suffix in ALLOWED_AVATAR_EXTENSIONS else ".png"


class OAuthAvatarCacheService:
    def __init__(self, *, config: CloudraveConfig) -> None:
        self.config = config

    def root_for(self, provider: str) -> Path:
        return (self.config.root_dir / "data" / "oauth-avatar" / provider).resolve()

    def local_url(self, provider: str, filename: str) -> str:
        return f"/api/v1/oauth/{provider}/avatar/{filename}"

    @staticmethod
    def _filename_for(subject: str, ext: str) -> str:
        digest = hashlib.sha256(subject.strip().encode("utf-8")).hexdigest()[:24]
        return f"{digest}{ext}"

    async def cache(
        self,
        *,
        provider: str,
        subject: str,
        external_url: str | None,
    ) -> str | None:
        """Download ``external_url`` into the local cache.

        Returns the local URL path (``/api/v1/oauth/{provider}/avatar/{file}``)
        on success, or ``None`` when there is nothing to download or the remote
        request fails (so callers can fall back to the original external URL).
        """

        if not external_url:
            return None

        root = self.root_for(provider)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                response = await client.get(
                    external_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/122.0 Safari/537.36"
                        ),
                    },
                )
                response.raise_for_status()
                content = response.content
                content_type = response.headers.get("content-type", "")
        except (httpx.HTTPError, OSError, ValueError):
            return None

        if not content:
            return None

        filename = self._filename_for(subject, _extension_for(content_type, external_url))
        destination = (root / filename).resolve()
        if root not in destination.parents:
            return None

        try:
            if not destination.exists():
                destination.write_bytes(content)
        except OSError:
            return None

        return self.local_url(provider, filename)

    def get_avatar_file(self, provider: str, filename: str) -> FileResponse:
        root = self.root_for(provider)
        safe_name = Path(filename).name
        avatar_path = (root / safe_name).resolve()
        if root not in avatar_path.parents or not avatar_path.is_file():
            raise FileNotFoundError(safe_name)
        return FileResponse(avatar_path)
