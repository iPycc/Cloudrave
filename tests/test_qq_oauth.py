import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.log import CloudraveFormatter
from schemas.oauth import QQOAuthSettingsUpdate
from services.oauth.qq import QQOAuthSettingsService


class SettingsRepository:
    def __init__(self):
        self.value = {}

    async def get(self):
        return dict(self.value)

    async def update(self, value):
        self.value = dict(value)


@pytest.mark.asyncio
async def test_rejects_copied_review_status_without_overwriting_key():
    repository = SettingsRepository()
    service = QQOAuthSettingsService(
        repository=repository,
        config=SimpleNamespace(auth=SimpleNamespace(jwt_secret="test-secret")),
        site_url="https://cloud.example.com",
    )
    await service.update(QQOAuthSettingsUpdate(enabled=True, app_id="123", app_key="valid+key"))
    previous = dict(repository.value)
    for key in ("key+审核中...", "masked...", "key with spaces"):
        with pytest.raises(HTTPException) as exc:
            await service.update(QQOAuthSettingsUpdate(enabled=True, app_id="123", app_key=key))
        assert exc.value.status_code == 422
        assert repository.value == previous
    assert (await service.get_credentials()).app_key == "valid+key"


def test_httpx_request_log_redacts_oauth_credentials():
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1,
        'HTTP Request: GET %s "HTTP/1.1 200 OK"',
        ("https://graph.qq.com/token?client_secret=secret%2Bvalue&code=private-code&access_token=private-token&fmt=json",),
        None,
    )
    rendered = CloudraveFormatter(use_color=False).format(record)
    for secret in ("secret%2Bvalue", "private-code", "private-token"):
        assert secret not in rendered
    assert "fmt=json" in rendered
