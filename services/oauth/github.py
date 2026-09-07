"""GitHub OAuth settings, protocol adapter, and identity resolution."""

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
from repositories.oauth import GitHubOAuthSettingsRepository, OAuthIdentityRepository
from repositories.users import UserRepository
from schemas.oauth import GitHubOAuthAdminSettings, GitHubOAuthPublicSettings, GitHubOAuthSettingsUpdate
from services.site.settings import is_secure_context_url


GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"
GITHUB_SCOPE = "user:email"


@dataclass(frozen=True, slots=True)
class GitHubOAuthCredentials:
    client_id: str
    client_secret: str
    callback_url: str


@dataclass(frozen=True, slots=True)
class GitHubIdentityProfile:
    subject: str
    login: str
    email: str


class GitHubOAuthProtocolError(RuntimeError):
    pass


class GitHubIdentityNotLinkedError(RuntimeError):
    pass


class GitHubIdentityConflictError(RuntimeError):
    pass


class GitHubOAuthIdentityService:
    def __init__(
        self,
        *,
        identities: OAuthIdentityRepository,
        users: UserRepository,
    ) -> None:
        self.identities = identities
        self.users = users

    async def get_for_user(self, user: User) -> OAuthIdentity | None:
        return await self.identities.get_for_user(user.id, "github")

    async def unlink(self, user: User) -> OAuthIdentity | None:
        identity = await self.get_for_user(user)
        if identity is None:
            return None
        await self.identities.delete(identity)
        return identity

    async def link(self, user: User, profile: GitHubIdentityProfile) -> OAuthIdentity:
        identity = await self.identities.get("github", profile.subject)
        existing_for_user = await self.identities.get_for_user(user.id, "github")

        if identity is not None and identity.user_id != user.id:
            raise GitHubIdentityConflictError("GitHub identity belongs to another account")
        if existing_for_user is not None and existing_for_user.subject != profile.subject:
            raise GitHubIdentityConflictError("Cloudrave account already has another GitHub identity")

        linked_identity = identity or existing_for_user
        if linked_identity is None:
            return await self.identities.create(
                user_id=user.id,
                provider="github",
                subject=profile.subject,
                provider_username=profile.login,
                provider_email=profile.email,
            )
        return await self.identities.touch(
            linked_identity,
            provider_username=profile.login,
            provider_email=profile.email,
        )

    async def resolve_login(self, profile: GitHubIdentityProfile) -> User:
        identity = await self.identities.get("github", profile.subject)
        if identity is None:
            raise GitHubIdentityNotLinkedError("GitHub identity is not linked")

        user = await self.users.get_by_id(identity.user_id)
        if user is None:
            raise GitHubIdentityNotLinkedError("Linked Cloudrave account no longer exists")

        await self.identities.touch(
            identity,
            provider_username=profile.login,
            provider_email=profile.email,
        )
        return user


class GitHubOAuthSettingsService:
    def __init__(
        self,
        *,
        repository: GitHubOAuthSettingsRepository,
        config: CloudraveConfig,
        site_url: str,
    ) -> None:
        self.repository = repository
        self.config = config
        self.site_url = site_url.strip().rstrip("/")
        material = f"cloudrave:github-oauth:{config.auth.jwt_secret}".encode("utf-8")
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))

    async def get_admin(self) -> GitHubOAuthAdminSettings:
        raw = await self.repository.get()
        return GitHubOAuthAdminSettings(
            enabled=self._is_ready(raw),
            client_id=str(raw.get("client_id") or ""),
            client_secret_configured=bool(self._decrypt_secret(raw)),
            callback_url=self.callback_url,
        )

    async def get_public(self) -> GitHubOAuthPublicSettings:
        raw = await self.repository.get()
        return GitHubOAuthPublicSettings(enabled=self._is_ready(raw))

    async def update(self, payload: GitHubOAuthSettingsUpdate) -> GitHubOAuthAdminSettings:
        current = await self.repository.get()
        client_id = payload.client_id.strip()
        encrypted_secret = str(current.get("client_secret") or "")
        if payload.client_secret is not None and payload.client_secret.strip():
            encrypted_secret = self._fernet.encrypt(
                payload.client_secret.strip().encode("utf-8")
            ).decode("utf-8")

        if payload.enabled:
            if not self.site_url or not is_secure_context_url(self.site_url):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="启用 GitHub OAuth 前请先在站点设置中保存 HTTPS 网站地址",
                )
            if not client_id or not encrypted_secret or not self._decrypt_value(encrypted_secret):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="启用 GitHub OAuth 需要填写 Client ID 和 Client Secret",
                )

        await self.repository.update(
            {
                "enabled": payload.enabled,
                "client_id": client_id,
                "client_secret": encrypted_secret,
            }
        )
        return await self.get_admin()

    async def get_credentials(self) -> GitHubOAuthCredentials:
        raw = await self.repository.get()
        secret = self._decrypt_secret(raw)
        if not self._is_ready(raw) or not secret:
            raise GitHubOAuthProtocolError("GitHub OAuth is not configured")
        return GitHubOAuthCredentials(
            client_id=str(raw.get("client_id") or ""),
            client_secret=secret,
            callback_url=self.callback_url,
        )

    @property
    def callback_url(self) -> str:
        if not self.site_url:
            return ""
        return f"{self.site_url}/api/v1/oauth/github/callback"

    def _is_ready(self, raw: dict[str, object]) -> bool:
        return bool(
            raw.get("enabled")
            and str(raw.get("client_id") or "").strip()
            and self._decrypt_secret(raw)
            and self.callback_url
            and is_secure_context_url(self.site_url)
        )

    def _decrypt_secret(self, raw: dict[str, object]) -> str:
        return self._decrypt_value(str(raw.get("client_secret") or ""))

    def _decrypt_value(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError):
            return ""


class GitHubOAuthClient:
    @staticmethod
    def authorization_url(
        credentials: GitHubOAuthCredentials,
        *,
        state: str,
        code_challenge: str,
    ) -> str:
        query = urlencode(
            {
                "client_id": credentials.client_id,
                "redirect_uri": credentials.callback_url,
                "scope": GITHUB_SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{GITHUB_AUTHORIZE_URL}?{query}"

    async def exchange_code(
        self,
        credentials: GitHubOAuthCredentials,
        *,
        code: str,
        code_verifier: str,
    ) -> GitHubIdentityProfile:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Cloudrave",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                token_response = await client.post(
                    GITHUB_TOKEN_URL,
                    headers=headers,
                    data={
                        "client_id": credentials.client_id,
                        "client_secret": credentials.client_secret,
                        "code": code,
                        "redirect_uri": credentials.callback_url,
                        "code_verifier": code_verifier,
                    },
                )
                token_response.raise_for_status()
                token_payload = token_response.json()
                if not isinstance(token_payload, dict):
                    raise GitHubOAuthProtocolError("GitHub returned an invalid token response")
                access_token = str(token_payload.get("access_token") or "")
                if not access_token:
                    raise GitHubOAuthProtocolError("GitHub did not return an access token")

                api_headers = {**headers, "Authorization": f"Bearer {access_token}"}
                user_response = await client.get(GITHUB_USER_URL, headers=api_headers)
                emails_response = await client.get(GITHUB_EMAILS_URL, headers=api_headers)
                user_response.raise_for_status()
                emails_response.raise_for_status()
                user_payload = user_response.json()
                emails_payload = emails_response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise GitHubOAuthProtocolError("GitHub OAuth request failed") from exc

        if not isinstance(user_payload, dict):
            raise GitHubOAuthProtocolError("GitHub returned an invalid user response")

        subject = str(user_payload.get("id") or "").strip()
        login = str(user_payload.get("login") or "").strip()
        verified_emails = [
            item
            for item in emails_payload
            if isinstance(item, dict) and item.get("verified") and item.get("email")
        ] if isinstance(emails_payload, list) else []
        primary = next((item for item in verified_emails if item.get("primary")), None)
        selected = primary or (verified_emails[0] if verified_emails else None)
        email = str((selected or {}).get("email") or "").strip().lower()
        if not subject or not login or not email:
            raise GitHubOAuthProtocolError("GitHub identity is incomplete")
        return GitHubIdentityProfile(subject=subject, login=login, email=email)
