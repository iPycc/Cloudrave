from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import alibabacloud_oss_v2 as oss
import pytest
from fastapi import HTTPException

from schemas.user_mount import OssDomainRequest
from store.oss.domains import manage_domains
from store.oss.driver import AliyunOssAdapter
from store.manager import _PolicyContext
from services.user.mount import UserMountService


def policy(mode="origin", domain="https://files.example.com", endpoint=None):
    return SimpleNamespace(bucket_name="example-bucket", region="cn-hangzhou", endpoint=endpoint,
                           base_prefix="", credentials={"access_key_id": "test-id", "access_key_secret": "test-secret", "security_token": "test-token"},
                           presign_ttl_seconds=900, extra={"download_domain_type": mode, "download_domain": domain})


class DomainClient:
    def __init__(self):
        self.calls = []

    def create_cname_token(self, request):
        self.calls.append(request)
        return oss.CreateCnameTokenResult(cname_token=oss.CnameToken(token="verify-value", expire_time="2026-11-07T00:00:00Z"))

    def put_cname(self, request):
        self.calls.append(request)

    def delete_cname(self, request):
        self.calls.append(request)

    def list_cname(self, request):
        self.calls.append(request)
        return oss.ListCnameResult(cnames=[oss.CnameInfo(domain="files.example.com", status="Enabled")])


def test_domain_lifecycle_leaves_certificates_untouched():
    client = DomainClient()
    token = manage_domains(client, "bucket", "cn-hangzhou", "token", "https://files.example.com")
    assert token["txt_name"] == "_dnsauth.files.example.com"
    assert token["token"] == "verify-value"
    assert token["cname_target"] == "bucket.oss-cn-hangzhou.aliyuncs.com"
    manage_domains(client, "bucket", "cn-hangzhou", "bind", "files.example.com")
    manage_domains(client, "bucket", "cn-hangzhou", "unbind", "files.example.com")
    for request in client.calls:
        assert request.bucket_cname_configuration.cname.certificate_configuration is None
    assert manage_domains(client, "bucket", "cn-hangzhou", "list")["domains"] == [{"domain": "files.example.com", "status": "Enabled"}]
    with pytest.raises(ValueError):
        manage_domains(client, "bucket", "cn-hangzhou", "bind")


def test_domain_check_uses_management_host_and_does_not_mutate_saved_policy(monkeypatch):
    adapter = AliyunOssAdapter()
    saved = policy()
    context = _PolicyContext(saved, saved.credentials)
    client = DomainClient()
    original_client_for = adapter._client_for
    monkeypatch.setattr(adapter, "_client_for", lambda policy, download=False: original_client_for(policy, download=True) if download else client)
    monkeypatch.setattr(adapter, "list_objects", lambda *args, **kwargs: SimpleNamespace(objects=[SimpleNamespace(object_key="test file.txt")]))
    result = adapter.manage_domains(context, SimpleNamespace(id=1, root_path="", extra={}), "check", "https://files.example.com")
    assert result["bound"] is True
    assert urlsplit(result["test_url"]).hostname == "files.example.com"
    assert 295 <= int(parse_qs(urlsplit(result["test_url"]).query)["x-oss-expires"][0]) <= 300
    assert saved.extra["download_domain"] == "https://files.example.com"
    unbound = adapter.manage_domains(context, SimpleNamespace(id=1, root_path="", extra={}), "check", "https://other.example.com")
    assert unbound["bound"] is False
    assert "test_url" not in unbound
    assert context.extra == saved.extra
    assert all(isinstance(request, oss.ListCnameRequest) for request in client.calls)


@pytest.mark.asyncio
async def test_domain_operations_require_both_mount_and_policy_ownership():
    mount = SimpleNamespace(owner_id=1, policy_id=2)
    saved = SimpleNamespace(provider="aliyun_oss", extra={"owner_user_id": 1, "user_managed": True})
    adapter = SimpleNamespace(manage_domains=Mock(return_value={"domains": []}))
    manager = SimpleNamespace(get_adapter=Mock(return_value=adapter))
    service = UserMountService(session=None, mounts=SimpleNamespace(get_by_id=AsyncMock(return_value=mount)),
                               policies=SimpleNamespace(get_by_id=AsyncMock(return_value=saved)),
                               nodes=None, uploads=None, audits=None, storage_manager=manager)
    for user_id, expected in [(3, 404), (1, 403)]:
        if user_id == 1:
            saved.extra["owner_user_id"] = 3
        with pytest.raises(HTTPException) as error:
            await service.manage_oss_domains(None, SimpleNamespace(id=user_id), 1, OssDomainRequest())
        assert error.value.status_code == expected
    manager.get_adapter.assert_not_called()
    saved.extra["owner_user_id"] = 1
    assert await service.manage_oss_domains(None, SimpleNamespace(id=1), 1, OssDomainRequest()) == {"domains": []}
    adapter.manage_domains.assert_called_once()
    saved.provider = "tencent_cos"
    with pytest.raises(HTTPException) as error:
        await service.manage_oss_domains(None, SimpleNamespace(id=1), 1, OssDomainRequest())
    assert error.value.status_code == 400


@pytest.mark.parametrize("status", ["Disabled", "Deleted"])
def test_disabled_domain_does_not_list_or_sign_files(monkeypatch, status):
    adapter = AliyunOssAdapter()
    client = SimpleNamespace(list_cname=lambda _: oss.ListCnameResult(cnames=[oss.CnameInfo(domain="files.example.com", status=status)]))
    monkeypatch.setattr(adapter, "_client_for", lambda _: client)
    sample = Mock(side_effect=AssertionError("must not list objects for an inactive binding"))
    monkeypatch.setattr(adapter, "list_objects", sample)
    result = adapter.manage_domains(policy(), SimpleNamespace(), "check", "files.example.com")
    assert "test_url" not in result
    sample.assert_not_called()


def test_domain_request_rejects_certificate_operations_and_invalid_domains():
    with pytest.raises(ValueError):
        OssDomainRequest(action="delete-certificate")
    with pytest.raises(ValueError):
        OssDomainRequest(action="bind", domain="https://example.com/path")
