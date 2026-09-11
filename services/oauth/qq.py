"""QQ OAuth settings, protocol adapter, and identity resolution."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from core.cfg import CloudraveConfig
from models.tables import OAuthIdentity, User
from repositories.oauth import OAuthIdentityRepository, QQOAuthSettingsRepository
from repositories.users import UserRepository
from schemas.oauth import QQOAuthAdminSettings, QQOAuthPublicSettings, QQOAuthSettingsUpdate
from services.site.settings import is_secure_context_url


QQ_AUTHORIZE_URL = "https://graph.qq.com/oauth2.0/authorize"
QQ_TOKEN_URL = "https://graph.qq.com/oauth2.0/token"
QQ_OPENID_URL = "https://graph.qq.com/oauth2.0/me"
QQ_USERINFO_URL = "https://graph.qq.com/user/get_user_info"
QQ_SCOPE = "get_user_info"


@dataclass(frozen=True, slots=True)
class QQOAuthCredentials:
    app_id: str
    app_key: str
    callback_url: str


@dataclass(frozen=True, slots=True)
class QQIdentityProfile:
    subject: str
    nickname: str
    avatar_url: str | None = None


class QQOAuthProtocolError(RuntimeError):
    pass


class QQIdentityNotLinkedError(RuntimeError):
    pass


class QQIdentityConflictError(RuntimeError):
    pass


class QQOAuthIdentityService:
    def __init__(
        self,
        *,
        identities: OAuthIdentityRepository,
        users: UserRepository,
    ) -> None:
        self.identities = identities
        self.users = users

    async def get_for_user(self, user: User) -> OAuthIdentity | None:
        return await self.identities.get_for_user(user.id, "qq")

    async def unlink(self, user: User) -> OAuthIdentity | None:
        identity = await self.get_for_user(user)
        if identity is None:
            return None
        await self.identities.delete(identity)
        return identity

    async def link(
        self,
        user: User,
        profile: QQIdentityProfile,
        *,
        provider_avatar_url: str | None = None,
    ) -> OAuthIdentity:
        identity = await self.identities.get("qq", profile.subject)
        existing_for_user = await self.identities.get_for_user(user.id, "qq")

        if identity is not None and identity.user_id != user.id:
            raise QQIdentityConflictError("QQ identity belongs to another account")
        if existing_for_user is not None and existing_for_user.subject != profile.subject:
            raise QQIdentityConflictError("Cloudrave account already has another QQ identity")

        linked_identity = identity or existing_for_user
        if linked_identity is None:
            return await self.identities.create(
                user_id=user.id,
                provider="qq",
                subject=profile.subject,
                provider_username=profile.nickname,
                provider_email=None,
                provider_avatar_url=provider_avatar_url,
            )
        return await self.identities.touch(
            linked_identity,
            provider_username=profile.nickname,
            provider_email=None,
            provider_avatar_url=provider_avatar_url,
        )

    async def resolve_login(
        self,
        profile: QQIdentityProfile,
        *,
        provider_avatar_url: str | None = None,
    ) -> User:
        identity = await self.identities.get("qq", profile.subject)
        if identity is None:
            raise QQIdentityNotLinkedError("QQ identity is not linked")

        user = await self.users.get_by_id(identity.user_id)
        if user is None:
            raise QQIdentityNotLinkedError("Linked Cloudrave account no longer exists")

        await self.identities.touch(
            identity,
            provider_username=profile.nickname,
            provider_email=None,
            provider_avatar_url=provider_avatar_url,
        )
        return user


class QQOAuthSettingsService:
    def __init__(
        self,
        *,
        repository: QQOAuthSettingsRepository,
        config: CloudraveConfig,
        site_url: str,
    ) -> None:
        self.repository = repository
        self.site_url = site_url.strip().rstrip("/")
        material = f"cloudrave:qq-oauth:{config.auth.jwt_secret}".encode("utf-8")
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))

    async def get_admin(self) -> QQOAuthAdminSettings:
        raw = await self.repository.get()
        return QQOAuthAdminSettings(
            enabled=self._is_ready(raw),
            app_id=str(raw.get("app_id") or ""),
            app_key_configured=bool(self._decrypt_secret(raw)),
            callback_url=self.callback_url,
        )

    async def get_public(self) -> QQOAuthPublicSettings:
        raw = await self.repository.get()
        return QQOAuthPublicSettings(enabled=self._is_ready(raw))

    async def update(self, payload: QQOAuthSettingsUpdate) -> QQOAuthAdminSettings:
        current = await self.repository.get()
        app_id = payload.app_id.strip()
        encrypted_secret = str(current.get("app_key") or "")
        if payload.app_key is not None and payload.app_key.strip():
            encrypted_secret = self._fernet.encrypt(
                payload.app_key.strip().encode("utf-8")
            ).decode("utf-8")

        if payload.enabled:
            if not self.site_url or not is_secure_context_url(self.site_url):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="启用 QQ OAuth 前请先在站点设置中保存 HTTPS 网站地址",
                )
            if not app_id or not encrypted_secret or not self._decrypt_value(encrypted_secret):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="启用 QQ OAuth 需要填写 App ID 和 App Key",
                )

        await self.repository.update(
            {
                "enabled": payload.enabled,
                "app_id": app_id,
                "app_key": encrypted_secret,
            }
        )
        return await self.get_admin()

    async def get_credentials(self) -> QQOAuthCredentials:
        raw = await self.repository.get()
        secret = self._decrypt_secret(raw)
        if not self._is_ready(raw) or not secret:
            raise QQOAuthProtocolError("QQ OAuth is not configured")
        return QQOAuthCredentials(
            app_id=str(raw.get("app_id") or ""),
            app_key=secret,
            callback_url=self.callback_url,
        )

    @property
    def callback_url(self) -> str:
        if not self.site_url:
            return ""
        return f"{self.site_url}/api/v1/oauth/qq/callback"

    def _is_ready(self, raw: dict[str, object]) -> bool:
        return bool(
            raw.get("enabled")
            and str(raw.get("app_id") or "").strip()
            and self._decrypt_secret(raw)
            and self.callback_url
            and is_secure_context_url(self.site_url)
        )

    def _decrypt_secret(self, raw: dict[str, object]) -> str:
        return self._decrypt_value(str(raw.get("app_key") or ""))

    def _decrypt_value(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError):
            return ""


class QQOAuthClient:
    @staticmethod
    def authorization_url(credentials: QQOAuthCredentials, *, state: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": credentials.app_id,
                "redirect_uri": credentials.callback_url,
                "scope": QQ_SCOPE,
                "state": state,
            }
        )
        return f"{QQ_AUTHORIZE_URL}?{query}"

    async def exchange_code(
        self,
        credentials: QQOAuthCredentials,
        *,
        code: str,
    ) -> QQIdentityProfile:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                token_response = await client.get(
                    QQ_TOKEN_URL,
                    params={
                        "grant_type": "authorization_code",
                        "client_id": credentials.app_id,
                        "client_secret": credentials.app_key,
                        "code": code,
                        "redirect_uri": credentials.callback_url,
                        "fmt": "json",
                    },
                )
                token_response.raise_for_status()
                token_payload = token_response.json()
                if not isinstance(token_payload, dict) or token_payload.get("error"):
                    raise QQOAuthProtocolError("QQ returned an invalid token response")
                access_token = str(token_payload.get("access_token") or "").strip()
                if not access_token:
                    raise QQOAuthProtocolError("QQ did not return an access token")

                openid_response = await client.get(
                    QQ_OPENID_URL,
                    params={"access_token": access_token, "fmt": "json"},
                )
                openid_response.raise_for_status()
                openid_payload = openid_response.json()
                if not isinstance(openid_payload, dict) or openid_payload.get("error"):
                    raise QQOAuthProtocolError("QQ returned an invalid OpenID response")
                openid = str(openid_payload.get("openid") or "").strip()
                if not openid:
                    raise QQOAuthProtocolError("QQ did not return an OpenID")

                user_response = await client.get(
                    QQ_USERINFO_URL,
                    params={
                        "access_token": access_token,
                        "oauth_consumer_key": credentials.app_id,
                        "openid": openid,
                        "fmt": "json",
                    },
                )
                user_response.raise_for_status()
                user_payload = user_response.json()
        except QQOAuthProtocolError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise QQOAuthProtocolError("QQ OAuth request failed") from exc

        if not isinstance(user_payload, dict) or user_payload.get("ret") != 0:
            raise QQOAuthProtocolError("QQ returned an invalid user response")
        nickname = str(user_payload.get("nickname") or "").strip()
        if not nickname:
            raise QQOAuthProtocolError("QQ identity is incomplete")
        avatar_url = next(
            (
                str(user_payload.get(key) or "").strip()
                for key in ("figureurl_qq_2", "figureurl_qq_1", "figureurl_2", "figureurl_1")
                if str(user_payload.get(key) or "").strip()
            ),
            None,
        )
        return QQIdentityProfile(
            subject=f"{credentials.app_id}:{openid}",
            nickname=nickname,
            avatar_url=avatar_url,
        )
