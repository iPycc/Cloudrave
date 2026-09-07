"""OAuth provider API contracts."""

from __future__ import annotations

from pydantic import Field

from schemas.auth import AuthSchema


class GitHubOAuthPublicSettings(AuthSchema):
    enabled: bool = False
    authorize_url: str = "/api/v1/oauth/github/start"


class GitHubOAuthConnection(AuthSchema):
    enabled: bool = False
    linked: bool = False
    provider_username: str | None = None
    provider_avatar_url: str | None = None
    link_url: str = "/api/v1/oauth/github/link"


class GitHubOAuthAdminSettings(AuthSchema):
    enabled: bool = False
    client_id: str = ""
    client_secret_configured: bool = False
    callback_url: str = ""


class GitHubOAuthSettingsUpdate(AuthSchema):
    enabled: bool
    client_id: str = Field(default="", max_length=255)
    client_secret: str | None = Field(default=None, max_length=512)


class GoogleOAuthPublicSettings(AuthSchema):
    enabled: bool = False
    authorize_url: str = "/api/v1/oauth/google/start"


class GoogleOAuthConnection(AuthSchema):
    enabled: bool = False
    linked: bool = False
    provider_username: str | None = None
    provider_avatar_url: str | None = None
    link_url: str = "/api/v1/oauth/google/link"


class GoogleOAuthAdminSettings(AuthSchema):
    enabled: bool = False
    client_id: str = ""
    client_secret_configured: bool = False
    callback_url: str = ""


class GoogleOAuthSettingsUpdate(AuthSchema):
    enabled: bool
    client_id: str = Field(default="", max_length=255)
    client_secret: str | None = Field(default=None, max_length=512)
