"""TOTP two-factor authentication service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
import pyotp
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.tables import User
from repositories.audit_logs import AuditLogRepository
from repositories.passkeys import PasskeyRepository
from repositories.site_settings import SiteSettingsRepository
from repositories.users import UserRepository
from security.jwt import TokenService
from security.password import PasswordService
from core.cfg import CloudraveConfig
from web.client_ip import get_client_ip


def _derive_fernet_key(secret: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


class TOTPService:
    """Handles TOTP secret generation, verification and re-authentication."""

    SETUP_TOKEN_TTL_SECONDS = 600
    LOGIN_TOKEN_TTL_SECONDS = 300

    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        audits: AuditLogRepository,
        passkeys: PasskeyRepository,
        password_service: PasswordService,
        token_service: TokenService,
        config: CloudraveConfig,
        site_settings: SiteSettingsRepository | None = None,
    ) -> None:
        self.session = session
        self.users = users
        self.audits = audits
        self.passkeys = passkeys
        self.password_service = password_service
        self.token_service = token_service
        self.config = config
        self.site_settings = site_settings
        self._totp_fernet = Fernet(_derive_fernet_key(config.auth.totp_secret_key))

    def _encrypt_secret(self, secret: str | None) -> str | None:
        if secret is None:
            return None
        return self._totp_fernet.encrypt(secret.encode("utf-8")).decode("utf-8")

    def _decrypt_secret(self, secret: str | None) -> str | None:
        if secret is None:
            return None
        try:
            return self._totp_fernet.decrypt(secret.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            return secret

    def generate_secret(self) -> str:
        return pyotp.random_base32()

    def build_provisioning_uri(self, secret: str, email: str, issuer: str) -> str:
        return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)

    def verify_code(self, secret: str, code: str) -> bool:
        if not secret or not code:
            return False
        plaintext_secret = self._decrypt_secret(secret)
        if not plaintext_secret:
            return False
        return pyotp.totp.TOTP(plaintext_secret).verify(code, valid_window=1)

    def verify_backup_code(self, user: User, code: str) -> bool:
        codes = user.totp_backup_codes or []
        code_hash = self._hash_backup_code(code)
        return any(hmac.compare_digest(code_hash, stored) for stored in codes)

    def consume_backup_code(self, user: User, code: str) -> list[str]:
        codes = list(user.totp_backup_codes or [])
        code_hash = self._hash_backup_code(code)
        next_codes = [stored for stored in codes if not hmac.compare_digest(code_hash, stored)]
        if len(next_codes) == len(codes):
            return codes
        return next_codes

    def generate_backup_codes(self, count: int = 8) -> tuple[list[str], list[str]]:
        raw = ["".join(secrets.choice("0123456789") for _ in range(8)) for _ in range(count)]
        hashed = [self._hash_backup_code(code) for code in raw]
        return raw, hashed

    def issue_setup_token(self, user_uid: str) -> str:
        return self.token_service.issue_2fa_token(
            sub=user_uid,
            purpose="totp_setup",
            ttl_seconds=self.SETUP_TOKEN_TTL_SECONDS,
        )

    def parse_setup_token(self, token: str) -> dict:
        return self.token_service.parse_2fa_token(token, purpose="totp_setup")

    def issue_login_token(self, user_uid: str, login_method: str) -> str:
        return self.token_service.issue_2fa_token(
            sub=user_uid,
            purpose=f"login_2fa:{login_method}",
            ttl_seconds=self.LOGIN_TOKEN_TTL_SECONDS,
        )

    def parse_login_token(self, token: str, login_method: str) -> dict:
        return self.token_service.parse_2fa_token(token, purpose=f"login_2fa:{login_method}")

    async def verify_password_reauth(self, user: User, password: str | None) -> bool:
        if not password:
            return False
        return self.password_service.verify_password(password, user.password_hash)

    async def verify_passkey_reauth(self, request: Request, user: User, ceremony_id: str, credential: dict) -> bool:
        from webauthn import verify_authentication_response
        from webauthn.helpers import base64url_to_bytes

        ceremony = await self.passkeys.get_active_ceremony(ceremony_id, "authentication")
        if ceremony is None or ceremony.user_id != user.id:
            return False

        credential_id = str(credential.get("id") or "").strip()
        record = await self.passkeys.get_by_credential_id(credential_id)
        if record is None or record.user_id != user.id:
            return False

        site_url = await self._get_site_url()
        expected_origin = self._resolve_origin(request, site_url)
        expected_rp_id = self._resolve_rp_id(request, site_url)
        try:
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(ceremony.challenge),
                expected_rp_id=expected_rp_id,
                expected_origin=expected_origin,
                credential_public_key=base64url_to_bytes(record.public_key),
                credential_current_sign_count=record.sign_count,
                require_user_verification=True,
            )
        except Exception:
            return False

        await self.passkeys.update_usage(record, sign_count=verification.new_sign_count)
        await self.passkeys.mark_ceremony_used(ceremony)
        return True

    async def _get_site_url(self) -> str | None:
        if self.site_settings is None:
            return None
        settings = await self.site_settings.get_settings()
        value = settings.get("site_url")
        return str(value).strip() if value else None

    def _resolve_origin(self, request: Request, site_url: str | None = None) -> str:
        from services.user.passkey_origin import resolve_passkey_origin

        return resolve_passkey_origin(request, self.config, site_url=site_url)

    def _resolve_rp_id(self, request: Request, site_url: str | None = None) -> str:
        from services.user.passkey_origin import resolve_passkey_rp_id

        return resolve_passkey_rp_id(self._resolve_origin(request, site_url))

    async def setup_totp(
        self,
        request: Request,
        actor: User,
        password: str | None,
        passkey_credential: dict | None,
        passkey_ceremony_id: str | None,
        issuer: str,
    ) -> dict:
        if actor.totp_enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="两步验证已开启")

        reauthenticated = False
        if password is not None:
            reauthenticated = await self.verify_password_reauth(actor, password)
        elif passkey_credential is not None and passkey_ceremony_id is not None:
            reauthenticated = await self.verify_passkey_reauth(request, actor, passkey_ceremony_id, passkey_credential)

        if not reauthenticated:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="身份验证失败，请重新验证")

        secret = self.generate_secret()
        setup_token = self.issue_setup_token(actor.uid)
        qr_uri = self.build_provisioning_uri(secret, actor.email, issuer)

        await self.audits.create(
            actor_id=actor.id,
            action="totp_setup_start",
            target=f"user:{actor.uid}",
            detail="开始设置两步验证",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )

        return {
            "setup_token": setup_token,
            "secret": secret,
            "qr_uri": qr_uri,
        }

    async def confirm_setup(
        self,
        request: Request,
        actor: User,
        setup_token: str,
        secret: str,
        code: str,
    ) -> dict:
        user = await self.users.get_by_uid(actor.uid)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

        if user.totp_enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="两步验证已开启")

        try:
            payload = self.parse_setup_token(setup_token)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="设置令牌已失效") from exc

        if payload.get("sub") != user.uid:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="设置令牌与当前账号不匹配")

        if not self.verify_code(secret, code):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="验证码错误")

        raw_backup_codes, hashed_backup_codes = self.generate_backup_codes()
        await self.users.update_totp(
            user,
            secret=self._encrypt_secret(secret),
            enabled=True,
            backup_codes=hashed_backup_codes,
        )

        await self.audits.create(
            actor_id=user.id,
            action="totp_setup_complete",
            target=f"user:{user.uid}",
            detail="两步验证已开启",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )

        return {"enabled": bool(user.totp_enabled), "backup_codes": raw_backup_codes}

    async def disable_totp(
        self,
        request: Request,
        actor: User,
        code: str,
        password: str | None,
        passkey_credential: dict | None,
        passkey_ceremony_id: str | None,
    ) -> None:
        user = await self.users.get_by_uid(actor.uid)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

        if not user.totp_enabled or not user.totp_secret:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="两步验证未开启")

        if not self.verify_code(user.totp_secret, code):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="验证码错误")

        reauthenticated = False
        if password is not None:
            reauthenticated = await self.verify_password_reauth(user, password)
        elif passkey_credential is not None and passkey_ceremony_id is not None:
            reauthenticated = await self.verify_passkey_reauth(request, user, passkey_ceremony_id, passkey_credential)

        if not reauthenticated:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="身份验证失败，请重新验证")

        await self.users.update_totp(
            user,
            secret=None,
            enabled=False,
            backup_codes=[],
        )

        await self.audits.create(
            actor_id=user.id,
            action="totp_disable",
            target=f"user:{user.uid}",
            detail="两步验证已关闭",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )

    async def verify_login(
        self,
        request: Request,
        login_token: str,
        code: str,
        login_method: str,
    ) -> User:
        try:
            payload = self.parse_login_token(login_token, login_method)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="登录令牌已失效") from exc

        user_uid = payload.get("sub")
        user = await self.users.get_by_uid(user_uid)
        if user is None or not user.totp_enabled or not user.totp_secret:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="两步验证状态异常")

        if not self.verify_code(self._decrypt_secret(user.totp_secret), code):
            await self.audits.create(
                actor_id=user.id,
                action="login",
                target=user.email,
                detail=f"两步验证失败 ({login_method})",
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_id=getattr(request.state, "request_id", None),
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="验证码错误")

        return user

    def _hash_backup_code(self, code: str) -> str:
        return hmac.new(
            self.config.auth.backup_codes_key.encode("utf-8"),
            code.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
