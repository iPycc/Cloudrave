from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from schemas.policy import StoragePolicyUpdate
from store.cos.driver import TencentCosAdapter


def policy(mode="default", auth="cos", domain="https://files.example.com"):
    return SimpleNamespace(bucket_name="test-1250000000", region="ap-guangzhou",
                           secret_id="test-id", secret_key="test-secret", session_token="temporary-token",
                           endpoint=None, presign_ttl_seconds=900,
                           extra={"download_domain_type": mode, "download_domain": domain, "download_cdn_auth": auth})


@pytest.mark.parametrize("mode,host,signed_host", [
    ("default", "test-1250000000.cos.ap-guangzhou.myqcloud.com", "host"),
    ("origin", "files.example.com", "host"),
    ("cdn", "files.example.com", ""),
])
def test_real_sdk_download_signing_and_upload_isolation(mode, host, signed_host):
    adapter = TencentCosAdapter()
    target = policy(mode)
    mount = SimpleNamespace(id=1, root_path="", extra={})
    result = adapter.build_download_url(target, mount, "中文/a b+#.txt", download_name="中文.txt")
    url = urlsplit(result.url)
    query = parse_qs(url.query, keep_blank_values=True)
    assert url.hostname == host
    assert "%E4%B8%AD" in url.path and "%20" in url.path and "%23" in url.path
    assert query["q-header-list"] == [signed_host]
    assert query["x-cos-security-token"] == ["temporary-token"]
    assert "filename*=UTF-8" in query["response-content-disposition"][0]
    assert result.expires_at is not None
    upload = adapter.presign_put(target, mount, "upload.txt")
    assert urlsplit(upload.url).hostname == "test-1250000000.cos.ap-guangzhou.myqcloud.com"
    assert adapter._client_for(target, download=True) is adapter._client_for(target, download=True)


def test_public_cdn_url_has_no_cos_credentials_or_expiry():
    adapter = TencentCosAdapter()
    result = adapter.build_download_url(policy("cdn", "public"), SimpleNamespace(id=1, root_path="", extra={}), "image.png", inline=True)
    assert result.url.startswith("https://files.example.com/")
    assert "q-sign" not in result.url and "temporary-token" not in result.url
    assert parse_qs(urlsplit(result.url).query)["response-content-disposition"] == ["inline"]
    assert result.expires_at is None


@pytest.mark.parametrize("domain", ["", "https://example.com/path", "https://user:pass@example.com", "ftp://example.com", "https://example.com?x=1", "https://example.com:443", "https://exa mple.com"])
def test_api_rejects_invalid_download_domain(domain):
    with pytest.raises(ValueError):
        StoragePolicyUpdate(extra={"download_domain_type": "origin", "download_domain": domain})


def test_disabling_custom_domain_restores_default_without_recreating_adapter():
    adapter = TencentCosAdapter()
    target = policy("origin", domain="files.example.com")
    mount = SimpleNamespace(id=1, root_path="", extra={})
    assert urlsplit(adapter.build_download_url(target, mount, "test").url).hostname == "files.example.com"
    target.extra["download_domain_type"] = "default"
    assert urlsplit(adapter.build_download_url(target, mount, "test").url).hostname.endswith("myqcloud.com")
