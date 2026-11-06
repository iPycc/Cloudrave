from types import SimpleNamespace
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from store.oss.download import domain_url, download_settings
from store.oss.driver import AliyunOssAdapter
from store.manager import StorageManager
from schemas.mount import mount_read


def policy(mode="origin", domain="https://files.example.com", endpoint=None):
    return SimpleNamespace(bucket_name="example-bucket", region="cn-hangzhou", endpoint=endpoint,
                           base_prefix="", credentials={"access_key_id": "test-id", "access_key_secret": "test-secret", "security_token": "test-token"},
                           presign_ttl_seconds=900, extra={"download_domain_type": mode, "download_domain": domain})


@pytest.mark.parametrize("mode,host", [("default", "example-bucket.oss-cn-hangzhou.aliyuncs.com"), ("origin", "files.example.com")])
def test_real_sdk_signature_and_upload_isolation(mode, host):
    adapter = AliyunOssAdapter()
    target = policy(mode)
    mount = SimpleNamespace(id=1, root_path="folder", extra={})
    result = adapter.build_download_url(target, mount, "中文/a b+#.txt", download_name="中文.txt", inline=True)
    url = urlsplit(result.url)
    query = parse_qs(url.query)
    assert url.hostname == host
    assert "%20" in url.path and "%23" in url.path and "%E4%B8%AD" in url.path
    assert query["x-oss-signature-version"] == ["OSS4-HMAC-SHA256"]
    assert query["x-oss-security-token"] == ["test-token"]
    assert query["response-content-disposition"][0].startswith("inline; filename*=UTF-8''")
    assert result.expires_at is not None
    assert urlsplit(adapter.presign_put(target, mount, "upload").url).hostname == "example-bucket.oss-cn-hangzhou.aliyuncs.com"
    assert adapter._client_for(target, download=True) is adapter._client_for(target, download=True)


def test_domain_changes_and_internal_endpoint_are_isolated():
    adapter = AliyunOssAdapter()
    target = policy(endpoint="https://oss-cn-hangzhou-internal.aliyuncs.com")
    mount = SimpleNamespace(id=1, root_path="", extra={})
    assert urlsplit(adapter.build_download_url(target, mount, "a").url).hostname == "files.example.com"
    assert "-internal" in adapter.presign_put(target, mount, "a").url
    target.extra["download_domain"] = "other.example.com"
    assert urlsplit(adapter.build_download_url(target, mount, "a").url).hostname == "other.example.com"
    target.extra["download_domain_type"] = "default"
    assert "-internal" in adapter.build_download_url(target, mount, "a").url
    with pytest.raises(ValueError):
        adapter.build_download_url(target, mount, "a", expires_in=0)


@pytest.mark.parametrize("domain", ["", "https://user:pass@example.com", "https://example.com/path", "https://example.com:443", "https://example.com?x=1", "https://example.com#x", "ftp://example.com", "https://-bad.example.com", "127.0.0.1", "https://exa mple.com", "https://example.com\\other", "https://localhost"])
def test_invalid_domain(domain):
    with pytest.raises(ValueError):
        domain_url(domain)


def test_domain_normalization_and_unsupported_cdn():
    assert domain_url(" Files.Example.com/ ") == "https://files.example.com"
    assert domain_url("https://文件.example.com").isascii()
    with pytest.raises(ValueError):
        download_settings({"download_domain_type": "cdn", "download_domain": "files.example.com"})


def test_oss_policy_rejects_cdn_mode_even_when_credentials_unchanged():
    manager = SimpleNamespace(definition_for=lambda _: SimpleNamespace(normalize_submitted_credentials=lambda _: {}))
    with pytest.raises(ValueError, match="default 或 origin"):
        StorageManager.credentials_from_payload(manager, "aliyun_oss", SimpleNamespace(extra={"download_domain_type": "cdn"}), current=policy())


def test_mount_read_uses_policy_download_settings_over_stale_mount_settings():
    target = policy()
    target.provider = "aliyun_oss"
    target.multipart_threshold_mb = 25
    target.part_size_mb = 25
    target.cors_auto_configured = False
    now = datetime.now(timezone.utc)
    mount = dict(id=1, owner_id=1, policy_id=1, name="OSS", mount_slug="oss",
                 created_at=now, updated_at=now,
                 extra={"download_domain_type": "origin", "download_domain": "https://stale.example.com"})
    assert mount_read(mount, target).extra["download_domain"] == "https://files.example.com"
    target.extra["download_domain_type"] = "default"
    assert mount_read(mount, target).extra["download_domain_type"] == "default"
