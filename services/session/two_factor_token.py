"""Short-lived token codec for pending two-factor login sessions."""

from __future__ import annotations

from security.jwt import TokenService


class TwoFactorTokenService:
    LOGIN_TOKEN_TTL_SECONDS = 300

    def __init__(self, token_service: TokenService) -> None:
        self.token_service = token_service

    def issue_login_token(self, user_uid: str, login_method: str) -> str:
        return self.token_service.issue_2fa_token(
            sub=user_uid,
            purpose=f"login_2fa:{login_method}",
            ttl_seconds=self.LOGIN_TOKEN_TTL_SECONDS,
        )

    def parse_login_token(self, token: str, login_method: str) -> dict:
        return self.token_service.parse_2fa_token(token, purpose=f"login_2fa:{login_method}")
