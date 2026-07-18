"""WebAuthn passkey services."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.models.enums import UserStatus
from app.models.tables import User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.passkeys import PasskeyRepository
from app.repositories.policies import StoragePolicyRepository
from app.repositories.sessions import SessionRepository
from app.repositories.site_settings import SiteSettingsRepository
from app.repositories.users import UserRepository
from app.schemas.auth import LoginResponse
from app.services.user.totp import TOTPService
from app.schemas.passkey import (
    PasskeyAuthenticationOptionsRequest,
    PasskeyAuthenticationResponse,
    PasskeyAuthenticationVerifyRequest,
    PasskeyListResponse,
    PasskeyOptionsResponse,
    PasskeyRead,
    PasskeyRenameRequest,
    PasskeyRegistrationOptionsRequest,
    PasskeyRegistrationVerifyRequest,
    PasskeyVerificationResponse,
)
from app.security.jwt import TokenService
from app.services.session.issue import IssueSessionService
from app.services.user.response import build_user_response
from app.services.user.storage import UserStorageProvisionService
from app.storage.manager import StorageManager
from config import CloudraveConfig


def _trim_to_seconds(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def _fingerprint(request: Request) -> str:
    return request.headers.get("x-device-fingerprint") or request.headers.get("user-agent", "Unknown device")


class PasskeyService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        audits: AuditLogRepository,
        passkeys: PasskeyRepository,
        policies: StoragePolicyRepository,
        mounts: BucketMountRepository,
        nodes: NodeRepository,
        token_service: TokenService,
        storage_manager: StorageManager,
        config: CloudraveConfig,
        site_settings: SiteSettingsRepository,
    ) -> None:
        self.session = session
        self.users = users
        self.sessions = sessions
        self.audits = audits
        self.passkeys = passkeys
        self.policies = policies
        self.mounts = mounts
        self.nodes = nodes
        self.token_service = token_service
        self.storage_manager = storage_manager
        self.config = config
        self.site_settings = site_settings

    async def list_for_user(self, actor: User) -> PasskeyListResponse:
        records = await self.passkeys.list_for_user(actor.id)
        return PasskeyListResponse(
            passkeys_enabled=bool(records),
            passkeys=[self._build_passkey_read(item) for item in records],
        )

    async def begin_registration(
        self,
        request: Request,
        actor: User,
        payload: PasskeyRegistrationOptionsRequest,
    ) -> PasskeyOptionsResponse:
        await self.passkeys.delete_expired_ceremonies()
        rp_id = await self._resolve_rp_id(request)
        records = await self.passkeys.list_for_user(actor.id)
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name=self.config.app.name,
            user_id=actor.uid.encode("utf-8"),
            user_name=actor.email,
            user_display_name=actor.username,
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id))
                for item in records
            ],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        ceremony = await self.passkeys.create_ceremony(
            ceremony_type="registration",
            challenge=self._to_base64url(options.challenge),
            user_id=actor.id,
        )
        return PasskeyOptionsResponse(
            ceremony_id=ceremony.id,
            options=json.loads(options_to_json(options)),
        )

    async def finish_registration(
        self,
        request: Request,
        actor: User,
        payload: PasskeyRegistrationVerifyRequest,
    ) -> PasskeyVerificationResponse:
        ceremony = await self.passkeys.get_active_ceremony(payload.ceremony_id, "registration")
        if ceremony is None or ceremony.user_id != actor.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="通行密钥注册挑战已失效")

        expected_origin = await self._resolve_origin(request)
        try:
            verification = verify_registration_response(
                credential=payload.credential,
                expected_challenge=base64url_to_bytes(ceremony.challenge),
                expected_origin=expected_origin,
                expected_rp_id=await self._resolve_rp_id(request),
                require_user_verification=True,
            )
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="通行密钥注册校验失败") from exc

        credential_id = self._to_base64url(verification.credential_id)
        existing = await self.passkeys.get_by_credential_id(credential_id)
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该通行密钥已绑定到账号")

        transports = payload.credential.get("response", {}).get("transports")
        record = await self.passkeys.create(
            user_id=actor.id,
            name=self._resolve_passkey_name(request, payload.name),
            credential_id=credential_id,
            public_key=self._to_base64url(verification.credential_public_key),
            sign_count=verification.sign_count,
            aaguid=str(verification.aaguid) if getattr(verification, "aaguid", None) else None,
            transports=[item for item in transports if isinstance(item, str)] if isinstance(transports, list) else [],
        )
        await self.passkeys.mark_ceremony_used(ceremony)
        await self.audits.create(
            actor_id=actor.id,
            action="passkey_register",
            target=f"passkey:{record.id}",
            detail=json.dumps({"name": record.name}, ensure_ascii=False),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return PasskeyVerificationResponse(passkey=self._build_passkey_read(record))

    async def delete_for_user(self, request: Request, actor: User, passkey_id: int) -> None:
        record = await self.passkeys.get_for_user(actor.id, passkey_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="通行密钥不存在")
        await self.passkeys.delete(record)
        await self.audits.create(
            actor_id=actor.id,
            action="passkey_delete",
            target=f"passkey:{passkey_id}",
            detail=json.dumps({"name": record.name}, ensure_ascii=False),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )

    async def rename_for_user(
        self,
        request: Request,
        actor: User,
        passkey_id: int,
        payload: PasskeyRenameRequest,
    ) -> PasskeyRead:
        record = await self.passkeys.get_for_user(actor.id, passkey_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="通行密钥不存在")

        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="通行密钥名称不能为空")

        record = await self.passkeys.rename(record, name)
        await self.audits.create(
            actor_id=actor.id,
            action="passkey_rename",
            target=f"passkey:{passkey_id}",
            detail=json.dumps({"name": record.name}, ensure_ascii=False),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return self._build_passkey_read(record)

    async def begin_authentication(
        self,
        request: Request,
        payload: PasskeyAuthenticationOptionsRequest,
    ) -> PasskeyOptionsResponse:
        await self.passkeys.delete_expired_ceremonies()
        email_hint = payload.email.strip().lower() if payload.email else None
        allow_credentials: list[PublicKeyCredentialDescriptor] = []
        user_id: int | None = None

        if email_hint:
            user = await self.users.get_by_email(email_hint)
            if user is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
            records = await self.passkeys.list_for_user(user.id)
            if not records:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="该账号尚未绑定通行密钥")
            allow_credentials = [
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id))
                for item in records
            ]
            user_id = user.id

        options = generate_authentication_options(
            rp_id=await self._resolve_rp_id(request),
            allow_credentials=allow_credentials or None,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        ceremony = await self.passkeys.create_ceremony(
            ceremony_type="authentication",
            challenge=self._to_base64url(options.challenge),
            user_id=user_id,
            email_hint=email_hint,
        )
        return PasskeyOptionsResponse(
            ceremony_id=ceremony.id,
            options=json.loads(options_to_json(options)),
        )

    async def finish_authentication(
        self,
        request: Request,
        payload: PasskeyAuthenticationVerifyRequest,
    ) -> PasskeyAuthenticationResponse:
        ceremony = await self.passkeys.get_active_ceremony(payload.ceremony_id, "authentication")
        if ceremony is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="通行密钥登录挑战已失效")

        credential_id = str(payload.credential.get("id") or "").strip()
        record = await self.passkeys.get_by_credential_id(credential_id)
        if record is None:
            await self._record_login_attempt(request, None, ceremony.email_hint or "", "failure", None)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="通行密钥验证失败")

        if ceremony.user_id is not None and ceremony.user_id != record.user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="通行密钥与当前登录挑战不匹配")

        expected_origin = await self._resolve_origin(request)
        try:
            verification = verify_authentication_response(
                credential=payload.credential,
                expected_challenge=base64url_to_bytes(ceremony.challenge),
                expected_rp_id=await self._resolve_rp_id(request),
                expected_origin=expected_origin,
                credential_public_key=base64url_to_bytes(record.public_key),
                credential_current_sign_count=record.sign_count,
                require_user_verification=True,
            )
        except Exception as exc:
            await self._record_login_attempt(request, None, ceremony.email_hint or "", "failure", None)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="通行密钥验证失败") from exc

        user = await self.users.get_by_id(record.user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
        if user.status != UserStatus.ACTIVE:
            await self._record_login_attempt(request, user, user.email, "disabled", None)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用")

        await UserStorageProvisionService(
            policies=self.policies,
            mounts=self.mounts,
            nodes=self.nodes,
            storage_manager=self.storage_manager,
        ).ensure_default_local_storage(user)

        if user.totp_enabled and user.totp_secret:
            two_factor_token = TOTPService(
                session=self.session,
                users=self.users,
                audits=self.audits,
                passkeys=None,  # type: ignore[arg-type]
                password_service=None,  # type: ignore[arg-type]
                token_service=self.token_service,
                config=self.config,
            ).issue_login_token(user.uid, login_method="passkey")
            await self.passkeys.update_usage(record, sign_count=verification.new_sign_count)
            await self.passkeys.mark_ceremony_used(ceremony)
            return PasskeyAuthenticationResponse(
                login=LoginResponse(
                    user=build_user_response(user),
                    token=None,  # type: ignore[arg-type]
                    requires_2fa=True,
                    two_factor_token=two_factor_token,
                    message="请输入两步验证码",
                )
            )

        session_record, token_pair = await IssueSessionService(
            session=self.session,
            sessions=self.sessions,
            token_service=self.token_service,
        ).issue_for_user(request, user)
        await self.passkeys.update_usage(record, sign_count=verification.new_sign_count)
        await self.passkeys.mark_ceremony_used(ceremony)
        await self._record_login_attempt(request, user, user.email, "success", session_record.id)

        return PasskeyAuthenticationResponse(
            login=LoginResponse(user=build_user_response(user), token=token_pair)
        )

    async def _record_login_attempt(
        self,
        request: Request,
        user: User | None,
        identifier: str,
        result: str,
        session_id: str | None,
    ) -> None:
        await self.audits.create(
            actor_id=user.id if user else None,
            action="login",
            target=(identifier or "passkey").lower(),
            detail=json.dumps(
                {
                    "method": "通行密钥登录",
                    "result": result,
                    "identifier": identifier,
                    "device_fingerprint": _fingerprint(request),
                    "session_id": session_id,
                },
                ensure_ascii=False,
            ),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )

    def _build_passkey_read(self, record) -> PasskeyRead:
        return PasskeyRead(
            id=record.id,
            name=record.name,
            created_at=_trim_to_seconds(record.created_at),
            last_used_at=_trim_to_seconds(record.last_used_at),
            transports=list(record.transports or []),
        )

    async def _resolve_origin(self, request: Request) -> str:
        origin = (request.headers.get("origin") or "").strip()
        if not origin:
            referer = (request.headers.get("referer") or "").strip()
            if referer:
                parsed = urlsplit(referer)
                origin = f"{parsed.scheme}://{parsed.netloc}"
        if not origin:
            settings = await self.site_settings.get_settings()
            origin = (settings.get("site_url") or "").strip()
        if not origin:
            origin = self.config.app.public_base_url.strip()
        if not origin:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无法识别当前 WebAuthn 来源")

        allowed_origins = set(self.config.app.cors_origins or ())
        if self.config.app.cors_origins and origin not in allowed_origins:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前来源未被允许使用通行密钥")
        return origin.rstrip("/")

    async def _resolve_rp_id(self, request: Request) -> str:
        parsed = urlsplit(await self._resolve_origin(request))
        if not parsed.hostname:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无法识别当前 WebAuthn 域名")
        return parsed.hostname

    def _to_base64url(self, value: bytes) -> str:
        import base64

        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def _resolve_passkey_name(self, request: Request, explicit_name: str | None) -> str:
        candidate = (explicit_name or "").strip()
        if candidate:
            return candidate
        return _build_passkey_device_name(request.headers.get("user-agent"))


def _build_passkey_device_name(user_agent: str | None) -> str:
    os_name = _detect_os_name(user_agent)
    browser_name = _detect_browser_name(user_agent)
    return f"{os_name}上的{browser_name}"


def _detect_os_name(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if "iphone" in ua:
        return "iPhone"
    if "ipad" in ua:
        return "iPad"
    if "android" in ua:
        return "Android"
    if "mac os x" in ua or "macintosh" in ua:
        return "Mac"
    if "windows nt" in ua:
        return "Windows"
    if "linux" in ua:
        return "Linux"
    return "当前设备"


def _detect_browser_name(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if "edg/" in ua:
        return "Edge"
    if "opr/" in ua or "opera/" in ua:
        return "Opera"
    if "firefox/" in ua:
        return "Firefox"
    if "crios/" in ua or "chrome/" in ua:
        return "Chrome"
    if "safari/" in ua and "chrome/" not in ua and "crios/" not in ua:
        return "Safari"
    return "浏览器"
