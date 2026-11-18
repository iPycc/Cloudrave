from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi import HTTPException

from schemas.upload import UploadSessionCreate
from services.explorer.up_plan import make_plan
from store.limits import MIB
from store.oss.driver import AliyunOssAdapter


@pytest.fixture
def oss_plan_args():
    adapter = AliyunOssAdapter()
    return dict(
        uploads=SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id="session"))),
        storage_manager=SimpleNamespace(get_adapter=lambda _: adapter),
        policy=SimpleNamespace(
            bucket_name="example-bucket",
            region="cn-guangzhou",
            credentials={"access_key_id": "test-id", "access_key_secret": "test-secret"},
            multipart_threshold_mb=16,
            part_size_mb=16,
            extra={"download_domain_type": "origin", "download_domain": "https://files.example.com"},
        ),
        mount=SimpleNamespace(id=1, owner_id=1, root_path=""),
        parent=None,
        file_name="archive.xip",
        object_key="blob/archive.xip",
        payload=UploadSessionCreate(mount_id=1, file_name="archive.xip", size=64 * MIB),
    )


@pytest.fixture
def mock_oss(monkeypatch):
    def mock_response(status_code, body):
        sent = []

        def send(_session, request, **kwargs):
            sent.append(request)
            response = requests.Response()
            response.status_code = status_code
            response.headers["Content-Type"] = "application/xml"
            response.headers["Date"] = "Wed, 18 Nov 2026 09:51:16 GMT"
            response._content = body
            response._content_consumed = True
            return response

        monkeypatch.setattr(requests.Session, "send", send)
        return sent

    return mock_response


async def test_oss_multipart_upload_signs_supported_metadata(mock_oss, oss_plan_args):
    sent = mock_oss(200, b"<InitiateMultipartUploadResult><UploadId>test-upload</UploadId></InitiateMultipartUploadResult>")
    oss_plan_args["payload"].checksum = "a" * 64

    _, plan = await make_plan(**oss_plan_args)

    [request] = sent
    assert request.method == "POST"
    assert urlsplit(request.url).hostname == "example-bucket.oss-cn-guangzhou.aliyuncs.com"
    assert {key.lower(): value for key, value in request.headers.items() if key.lower().startswith("x-oss-meta-")} == {
        "x-oss-meta-sha256": "a" * 64,
        "x-oss-meta-file-name": "archive.xip",
        "x-oss-meta-part-size": str(16 * MIB),
    }
    assert plan.upload_id == "test-upload"
    assert parse_qs(urlsplit(plan.upload_urls[0].url).query)["uploadId"] == ["test-upload"]


async def test_oss_multipart_signature_rejection_is_actionable(mock_oss, oss_plan_args):
    mock_oss(403, b"<Error><Code>SignatureDoesNotMatch</Code><Message>private upstream detail</Message></Error>")

    with pytest.raises(HTTPException) as failed:
        await make_plan(**oss_plan_args)

    assert failed.value.status_code == 502
    assert "Bucket 地域、Endpoint 和 AccessKey" in failed.value.detail
    assert "private upstream detail" not in failed.value.detail
    oss_plan_args["uploads"].create_session.assert_not_awaited()
