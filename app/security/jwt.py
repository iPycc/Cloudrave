"""JWT access token and refresh token helpers."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from config import AuthConfig


class AuthError(Exception):
    """Raised when credentials are invalid."""


@dataclass(slots=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    session_id: str


class TokenService:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config

    def issue_token_pair(self, *, user_uid: str, role: str, session_id: str, token_version: int) -> TokenBundle:
        now = datetime.now(UTC)
        access_expires_at = now + timedelta(minutes=self.config.access_token_ttl_minutes)
        refresh_expires_at = now + timedelta(days=self.config.refresh_token_ttl_days)
        refresh_secret = secrets.token_urlsafe(48)
        refresh_token = f"{session_id}.{refresh_secret}"
        access_token = jwt.encode(
            {
                "sub": user_uid,
                "sid": session_id,
                "role": role,
                "ver": token_version,
                "iss": self.config.jwt_issuer,
                "iat": int(now.timestamp()),
                "exp": int(access_expires_at.timestamp()),
            },
            self.config.jwt_secret,
            algorithm="HS256",
        )
        return TokenBundle(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
            session_id=session_id,
        )

    def parse_access_token(self, token: str) -> dict:
        try:
            return jwt.decode(
                token,
                self.config.jwt_secret,
                algorithms=["HS256"],
                issuer=self.config.jwt_issuer,
            )
        except jwt.PyJWTError as exc:
            raise AuthError("Invalid access token") from exc

    def parse_refresh_token(self, token: str) -> tuple[str, str]:
        if "." not in token:
            raise AuthError("Invalid refresh token")
        session_id, secret = token.split(".", 1)
        if not session_id or not secret:
            raise AuthError("Invalid refresh token")
        return session_id, secret

    def hash_refresh_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue_2fa_token(self, *, sub: str, purpose: str, ttl_seconds: int) -> str:
        return self.issue_scoped_token(sub=sub, purpose=purpose, ttl_seconds=ttl_seconds)

    def issue_scoped_token(
        self,
        *,
        sub: str,
        purpose: str,
        ttl_seconds: int,
        claims: dict[str, object] | None = None,
    ) -> str:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        payload: dict[str, object] = {
            "sub": sub,
            "purpose": purpose,
            "iss": self.config.jwt_issuer,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        payload.update(claims or {})
        return jwt.encode(
            payload,
            self.config.jwt_secret,
            algorithm="HS256",
        )

    def parse_2fa_token(self, token: str, purpose: str) -> dict:
        return self.parse_scoped_token(token, purpose)

    def parse_scoped_token(self, token: str, purpose: str) -> dict:
        try:
            payload = jwt.decode(
                token,
                self.config.jwt_secret,
                algorithms=["HS256"],
                issuer=self.config.jwt_issuer,
            )
        except jwt.PyJWTError as exc:
            raise AuthError("Invalid scoped token") from exc
        if payload.get("purpose") != purpose:
            raise AuthError("Invalid scoped token purpose")
        return payload
