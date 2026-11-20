"""Google OAuth settings, protocol adapter, and identity resolution."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from core.cfg import CloudraveConfig
from models.tables import OAuthIdentity, User
from repositories.oauth import GoogleOAuthSettingsRepository, OAuthIdentityRepository
from repositories.users import UserRepository
from schemas.oauth import GoogleOAuthAdminSettings, GoogleOAuthPublicSettings, GoogleOAuthSettingsUpdate
from services.site.settings import is_secure_context_url


GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_SCOPE = "openid email profile"


@dataclass(frozen=True, slots=True)
class GoogleOAuthCredentials:
    client_id: str
    client_secret: str
    callback_url: str


@dataclass(frozen=True, slots=True)
class GoogleIdentityProfile:
    subject: str
    name: str
    email: str
    avatar_url: str | None = None


class GoogleOAuthProtocolError(RuntimeError):
    pass


class GoogleIdentityNotLinkedError(RuntimeError):
    pass


class GoogleIdentityConflictError(RuntimeError):
    pass


class GoogleOAuthIdentityService:
    def __init__(
        self,
        *,
        identities: OAuthIdentityRepository,
        users: UserRepository,
    ) -> None:
        self.identities = identities
        self.users = users

    async def get_for_user(self, user: User) -> OAuthIdentity | None:
        return await self.identities.get_for_user(user.id, "google")

    async def unlink(self, user: User) -> OAuthIdentity | None:
        identity = await self.get_for_user(user)
        if identity is None:
            return None
        await self.identities.delete(identity)
        return identity

    async def link(self, user: User, profile: GoogleIdentityProfile, *, provider_avatar_url: str | None = None) -> OAuthIdentity:
        identity = await self.identities.get("google", profile.subject)
        existing_for_user = await self.identities.get_for_user(user.id, "google")

        if identity is not None and identity.user_id != user.id:
            raise GoogleIdentityConflictError("Google identity belongs to another account")
        if existing_for_user is not None and existing_for_user.subject != profile.subject:
            raise GoogleIdentityConflictError("Cloudrave account already has another Google identity")

        linked_identity = identity or existing_for_user
        avatar_url = provider_avatar_url if provider_avatar_url is not None else profile.avatar_url
        if linked_identity is None:
            return await self.identities.create(
                user_id=user.id,
                provider="google",
                subject=profile.subject,
                provider_username=profile.name,
                provider_email=profile.email,
                provider_avatar_url=avatar_url,
            )
        return await self.identities.touch(
            linked_identity,
            provider_username=profile.name,
            provider_email=profile.email,
            provider_avatar_url=avatar_url,
        )

    async def resolve_login(self, profile: GoogleIdentityProfile, *, provider_avatar_url: str | None = None) -> User:
        identity = await self.identities.get("google", profile.subject)
        if identity is None:
            raise GoogleIdentityNotLinkedError("Google identity is not linked")

        user = await self.users.get_by_id(identity.user_id)
        if user is None:
            raise GoogleIdentityNotLinkedError("Linked Cloudrave account no longer exists")

        await self.identities.touch(
            identity,
            provider_username=profile.name,
            provider_email=profile.email,
            provider_avatar_url=provider_avatar_url if provider_avatar_url is not None else profile.avatar_url,
        )
        return user


class GoogleOAuthSettingsService:
    def __init__(
        self,
        *,
        repository: GoogleOAuthSettingsRepository,
        config: CloudraveConfig,
        site_url: str,
    ) -> None:
        self.repository = repository
        self.config = config
        self.site_url = site_url.strip().rstrip("/")
        material = f"cloudrave:google-oauth:{config.auth.jwt_secret}".encode("utf-8")
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))
        self._verification_key = material + b":verification"

    async def get_admin(self) -> GoogleOAuthAdminSettings:
        raw = await self.repository.get()
        client_id = str(raw.get("client_id") or "")
        secret = self._decrypt_secret(raw)
        credentials = GoogleOAuthCredentials(client_id, secret, self.callback_url)
        return GoogleOAuthAdminSettings(
            enabled=self._is_ready(raw),
            client_id=client_id,
            client_secret_configured=bool(secret),
            callback_url=self.callback_url,
            configuration_verified=bool(
                client_id and secret and is_secure_context_url(self.site_url)
                and self.configuration_fingerprint(credentials) == await self.repository.get_verified_config()
            ),
        )

    async def get_public(self) -> GoogleOAuthPublicSettings:
        raw = await self.repository.get()
        return GoogleOAuthPublicSettings(enabled=self._is_ready(raw))

    async def update(self, payload: GoogleOAuthSettingsUpdate) -> GoogleOAuthAdminSettings:
        current = await self.repository.get()
        client_id = payload.client_id.strip()
        old_secret = self._decrypt_secret(current)
        secret = (payload.client_secret or "").strip() or old_secret
        encrypted_secret = str(current.get("client_secret") or "")
        if secret != old_secret:
            encrypted_secret = self._fernet.encrypt(secret.encode("utf-8")).decode("utf-8")

        enabled_config = ""
        if payload.enabled:
            if not self.site_url or not is_secure_context_url(self.site_url):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="启用 Google OAuth 前请先在站点设置中保存 HTTPS 网站地址",
                )
            if not client_id or not secret:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="启用 Google OAuth 需要填写 Client ID 和 Client Secret",
                )
            fingerprint = self.configuration_fingerprint(GoogleOAuthCredentials(client_id, secret, self.callback_url))
            previous = current.get("enabled_config")
            staying_enabled = bool(
                current.get("enabled") and current.get("client_id") == client_id and old_secret == secret
                and (not previous or previous == fingerprint)
            )
            if await self.repository.get_verified_config() == fingerprint:
                enabled_config = fingerprint
            elif staying_enabled:
                enabled_config = str(previous or "")
            else:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先保存配置并通过 Google 登录测试")

        await self.repository.update(
            {
                "enabled": payload.enabled,
                "client_id": client_id,
                "client_secret": encrypted_secret,
                "enabled_config": enabled_config,
            }
        )
        return await self.get_admin()

    def configuration_fingerprint(self, credentials: GoogleOAuthCredentials) -> str:
        payload = json.dumps((credentials.client_id, credentials.client_secret, credentials.callback_url)).encode("utf-8")
        return hmac.digest(self._verification_key, payload, "sha256").hex()

    async def mark_verified(self, credentials: GoogleOAuthCredentials) -> bool:
        try:
            current = await self.get_credentials(allow_disabled=True)
        except GoogleOAuthProtocolError:
            return False
        if credentials != current:
            return False
        await self.repository.set_verified_config(self.configuration_fingerprint(credentials))
        return True

    async def get_credentials(self, *, allow_disabled: bool = False) -> GoogleOAuthCredentials:
        raw = await self.repository.get()
        secret = self._decrypt_secret(raw)
        if not self._is_ready(raw, allow_disabled=allow_disabled) or not secret:
            raise GoogleOAuthProtocolError("Google OAuth is not configured")
        return GoogleOAuthCredentials(
            client_id=str(raw.get("client_id") or ""),
            client_secret=secret,
            callback_url=self.callback_url,
        )

    @property
    def callback_url(self) -> str:
        if not self.site_url:
            return ""
        return f"{self.site_url}/api/v1/oauth/google/callback"

    def _is_ready(self, raw: dict[str, object], *, allow_disabled: bool = False) -> bool:
        client_id = str(raw.get("client_id") or "").strip()
        secret = self._decrypt_secret(raw)
        if not (client_id and secret and self.callback_url and is_secure_context_url(self.site_url)):
            return False
        if allow_disabled:
            return True
        enabled_config = raw.get("enabled_config")
        return bool(raw.get("enabled") and (
            not enabled_config
            or enabled_config == self.configuration_fingerprint(GoogleOAuthCredentials(client_id, secret, self.callback_url))
        ))

    def _decrypt_secret(self, raw: dict[str, object]) -> str:
        return self._decrypt_value(str(raw.get("client_secret") or ""))

    def _decrypt_value(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError):
            return ""


class GoogleOAuthClient:
    @staticmethod
    def authorization_url(
        credentials: GoogleOAuthCredentials,
        *,
        state: str,
        code_challenge: str,
    ) -> str:
        query = urlencode(
            {
                "client_id": credentials.client_id,
                "redirect_uri": credentials.callback_url,
                "response_type": "code",
                "scope": GOOGLE_SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"{GOOGLE_AUTHORIZE_URL}?{query}"

    async def exchange_code(
        self,
        credentials: GoogleOAuthCredentials,
        *,
        code: str,
        code_verifier: str,
    ) -> GoogleIdentityProfile:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                token_response = await client.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "client_id": credentials.client_id,
                        "client_secret": credentials.client_secret,
                        "code": code,
                        "code_verifier": code_verifier,
                        "grant_type": "authorization_code",
                        "redirect_uri": credentials.callback_url,
                    },
                )
                token_response.raise_for_status()
                token_payload = token_response.json()
                if not isinstance(token_payload, dict):
                    raise GoogleOAuthProtocolError("Google returned an invalid token response")
                access_token = str(token_payload.get("access_token") or "")
                if not access_token:
                    raise GoogleOAuthProtocolError("Google did not return an access token")

                user_response = await client.get(
                    GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                user_response.raise_for_status()
                user_payload = user_response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise GoogleOAuthProtocolError("Google OAuth request failed") from exc

        if not isinstance(user_payload, dict):
            raise GoogleOAuthProtocolError("Google returned an invalid user response")

        subject = str(user_payload.get("sub") or "").strip()
        email = str(user_payload.get("email") or "").strip().lower()
        name = str(user_payload.get("name") or email).strip()
        avatar_url = str(user_payload.get("picture") or "").strip() or None
        if not subject or not email or user_payload.get("email_verified") is not True:
            raise GoogleOAuthProtocolError("Google identity is incomplete or unverified")
        return GoogleIdentityProfile(
            subject=subject,
            name=name or email,
            email=email,
            avatar_url=avatar_url,
        )
