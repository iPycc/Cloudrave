"""Tencent COS adapter composed from focused object and upload operations."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

from store.port import StorageAdapterBase
from store.cos.cors import RULE_ID, configured_origins, merge_rule
from store.identity import physical_hash
from store.key import mount_prefix, physical_key, physical_list_prefix
from store.cos.obj import CosObjectMixin
from store.cos.upload import CosUploadMixin
from store.types import StorageCapability
from core.cfg import TencentCosConfig

CLIENT_CACHE_SIZE = 64

try:
    from qcloud_cos import CosConfig, CosS3Client

    _COS_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by deployments without optional dependency
    CosConfig = CosS3Client = None  # type: ignore[assignment,misc]
    _COS_SDK_AVAILABLE = False


@dataclass(frozen=True, slots=True)
class _Target:
    bucket: str
    region: str
    secret_id: str
    secret_key: str
    token: str | None
    endpoint: str | None
    scheme: str
    timeout: int
    retries: int
    pool_connections: int
    pool_size: int

    @property
    def cache_key(self) -> tuple[Any, ...]:
        credential_hash = sha256(
            f"{self.secret_id}\0{self.secret_key}\0{self.token or ''}".encode()
        ).hexdigest()
        return (
            self.bucket,
            self.region,
            credential_hash,
            self.endpoint,
            self.scheme,
            self.timeout,
            self.retries,
            self.pool_connections,
            self.pool_size,
        )


class TencentCosAdapter(CosObjectMixin, CosUploadMixin, StorageAdapterBase):
    provider_type = "tencent_cos"
    capabilities = StorageCapability(
        supports_presign=True,
        supports_multipart=True,
        supports_cors_autoconfig=True,
        supports_direct_write=False,
        supports_single_put=True,
        supports_mirror=True,
        supports_versioned_delete=True,
        supports_server_side_encryption=True,
        upload_transport="presigned",
        requires_part_etag=True,
    )

    def __init__(self, config: TencentCosConfig | None = None) -> None:
        self.config = config
        self._clients: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._clients_lock = Lock()

    def _target(self, policy: Any) -> _Target:
        extra = getattr(policy, "extra", None) or {}
        fallback = self.config or TencentCosConfig()
        bucket = str(getattr(policy, "bucket_name", "") or fallback.bucket).strip()
        region = str(getattr(policy, "region", "") or fallback.default_region).strip()
        secret_id = str(getattr(policy, "secret_id", "") or fallback.secret_id).strip()
        secret_key = str(getattr(policy, "secret_key", "") or fallback.secret_key).strip()
        token = str(getattr(policy, "session_token", "") or "").strip() or None
        if not bucket:
            raise RuntimeError("Tencent COS policy bucket_name is required")
        if not region:
            raise RuntimeError("Tencent COS policy region is required")
        if not secret_id or not secret_key:
            raise RuntimeError("Tencent COS policy credentials are incomplete")

        endpoint, endpoint_scheme = self._endpoint(getattr(policy, "endpoint", None))
        scheme = str(extra.get("scheme") or endpoint_scheme or "https").lower()
        if scheme not in {"http", "https"}:
            raise ValueError("Tencent COS scheme must be http or https")
        if str(extra.get("server_side_encryption", "none")).lower() not in {"", "none"} and scheme != "https":
            raise ValueError("Tencent COS server-side encryption requires HTTPS")
        return _Target(
            bucket=bucket,
            region=region,
            secret_id=secret_id,
            secret_key=secret_key,
            token=token,
            endpoint=endpoint,
            scheme=scheme,
            timeout=self._positive_int(extra.get("timeout_seconds", 30), "timeout_seconds"),
            retries=self._bounded_int(extra.get("max_retries", 3), "max_retries", 0, 10),
            pool_connections=self._positive_int(extra.get("pool_connections", 20), "pool_connections"),
            pool_size=self._positive_int(extra.get("pool_max_size", 50), "pool_max_size"),
        )

    def _client_for(self, policy: Any) -> Any:
        if not _COS_SDK_AVAILABLE:
            raise RuntimeError(
                "Tencent Cloud COS SDK is not installed; install cos-python-sdk-v5"
            )
        target = self._target(policy)
        with self._clients_lock:
            client = self._clients.get(target.cache_key)
            if client is None:
                config_kwargs: dict[str, Any] = {
                    "Region": target.region,
                    "SecretId": target.secret_id,
                    "SecretKey": target.secret_key,
                    "Token": target.token,
                    "Scheme": target.scheme,
                    "Timeout": target.timeout,
                    "PoolConnections": target.pool_connections,
                    "PoolMaxSize": target.pool_size,
                    "AllowRedirects": False,
                }
                if target.endpoint:
                    config_kwargs["Endpoint"] = target.endpoint
                client = CosS3Client(CosConfig(**config_kwargs), retry=target.retries)
                self._clients[target.cache_key] = client
                if len(self._clients) > CLIENT_CACHE_SIZE:
                    self._clients.popitem(last=False)
            else:
                self._clients.move_to_end(target.cache_key)
        return client

    def clear_client_cache(self) -> None:
        with self._clients_lock:
            self._clients.clear()

    def validate_completed_object(self, policy: Any, mount: Any, stored_object: Any, checksum: str | None = None) -> None:
        verified = self.head_object(policy, mount, stored_object.object_key, verify_encryption=True)
        super().validate_completed_object(policy, mount, verified, checksum)

    def _bucket(self, policy: Any) -> str:
        return self._target(policy).bucket

    def _region(self, policy: Any) -> str:
        return self._target(policy).region

    def _key(self, policy: Any, mount: Any, object_key: str) -> str:
        return physical_key(policy, mount, object_key)

    def _list_prefix(self, policy: Any, mount: Any, prefix: str = "") -> str:
        return physical_list_prefix(policy, mount, prefix)

    def _mount_prefix(self, policy: Any, mount: Any) -> str:
        return mount_prefix(policy, mount)

    def _ttl(self, policy: Any) -> int:
        value = getattr(policy, "presign_ttl_seconds", None)
        if value is None and self.config is not None:
            value = self.config.presign_ttl_seconds
        return self._positive_int(value or 900, "presign_ttl_seconds")

    def prepare_mount_root(self, mount: Any) -> Path:
        return Path(str(getattr(mount, "root_path", "")).strip("/\\"))

    def physical_id(self, policy: Any, mount: Any, object_key: str) -> str:
        target = self._target(policy)
        # COS bucket names include the APPID and identify the bucket independently
        # of an optional CDN/custom endpoint or duplicate policy row.
        return physical_hash(self.provider_type, target.bucket.lower(), self._key(policy, mount, object_key))

    def configure_cors(self, policy: Any, mount: Any) -> dict[str, Any]:
        _ = mount
        extra = dict(getattr(policy, "extra", None) or {})
        origins = configured_origins(policy)
        rule = {
            "ID": RULE_ID,
            "AllowedOrigin": origins,
            "AllowedMethod": ["GET", "HEAD", "PUT", "POST"],
            "AllowedHeader": ["*"],
            "ExposeHeader": [
                "ETag",
                "Content-Length",
                "Content-Range",
                "Accept-Ranges",
                "x-cos-request-id",
                "x-cos-version-id",
                "x-cos-server-side-encryption",
            ],
            "MaxAgeSeconds": int(extra.get("cors_max_age", 600)),
        }
        bucket = self._bucket(policy)
        rule_count = merge_rule(self._client_for(policy), bucket, rule)
        return {
            "status": "configured",
            "bucket": bucket,
            "origins": origins,
            "rule": rule,
            "rule_count": rule_count,
        }

    async def aconfigure_cors(self, policy: Any, mount: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.configure_cors, policy, mount)

    @staticmethod
    def _seconds(value: int) -> timedelta:
        return timedelta(seconds=value)

    @staticmethod
    def _endpoint(value: Any) -> tuple[str | None, str | None]:
        raw = str(value or "").strip().rstrip("/")
        if not raw:
            return None, None
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        host = parsed.netloc or parsed.path
        if (
            not host
            or "/" in host
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Tencent COS endpoint must be a host without a path")
        return host, parsed.scheme or None

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        result = int(value)
        if result <= 0:
            raise ValueError(f"{name} must be positive")
        return result

    @staticmethod
    def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
        result = int(value)
        if not minimum <= result <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return result
