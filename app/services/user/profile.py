"""User profile, avatar, password and activity services."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.schemas.common import MessageResponse
from app.schemas.user import (
    LoginActivityEntry,
    PasswordChangeRequest,
    UserPreferencesResponse,
    UserPreferencesUpdateRequest,
    UserProfileUpdateRequest,
)
from app.security.password import PasswordService
from app.services.user.response import build_user_response
from config import CloudraveConfig


ALLOWED_AVATAR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class UserProfileService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        audits: AuditLogRepository,
        password_service: PasswordService,
        config: CloudraveConfig,
    ) -> None:
        self.session = session
        self.users = users
        self.sessions = sessions
        self.audits = audits
        self.password_service = password_service
        self.config = config

    async def update_profile(self, request: Request, actor: User, payload: UserProfileUpdateRequest):
        user = await self._get_managed_user(actor.uid)
        normalized_email = payload.email.lower()
        normalized_username = payload.username.strip()

        existing_email = await self.users.get_by_email(normalized_email)
        if existing_email is not None and existing_email.id != user.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱已存在")

        existing_username = await self.users.get_by_username(normalized_username)
        if existing_username is not None and existing_username.id != user.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")

        try:
            user = await self.users.update_profile(user, email=normalized_email, username=normalized_username)
        except IntegrityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱或用户名已存在") from exc

        await self.audits.create(
            actor_id=user.id,
            action="profile_update",
            target=f"user:{user.uid}",
            detail=json.dumps({"email": normalized_email, "username": normalized_username}, ensure_ascii=False),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return build_user_response(user)

    async def upload_avatar(self, request: Request, actor: User, file: UploadFile):
        user = await self._get_managed_user(actor.uid)
        if not file.filename:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请选择头像文件")

        suffix = Path(file.filename).suffix.lower() or _guess_extension(file.content_type)
        if suffix not in ALLOWED_AVATAR_EXTENSIONS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不支持的头像文件类型")

        avatar_root = self.config.root_dir / "data" / "avatar" / user.uid
        avatar_root.mkdir(parents=True, exist_ok=True)
        avatar_root_resolved = avatar_root.resolve()

        if user.avatar_path:
            previous_path = (self.config.root_dir / "data" / user.avatar_path).resolve()
            if previous_path.exists() and avatar_root_resolved in previous_path.parents:
                previous_path.unlink()

        next_name = f"{uuid4().hex}{suffix}"
        next_path = (avatar_root / next_name).resolve()
        if avatar_root_resolved not in next_path.parents:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="头像路径无效")

        content = await file.read()
        next_path.write_bytes(content)

        user = await self.users.update_avatar_path(user, f"avatar/{user.uid}/{next_name}")
        await self.audits.create(
            actor_id=user.id,
            action="avatar_upload",
            target=f"user:{user.uid}",
            detail=json.dumps({"avatar": user.avatar_path}, ensure_ascii=False),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return build_user_response(user)

    async def change_password(self, request: Request, actor: User, payload: PasswordChangeRequest) -> MessageResponse:
        user = await self._get_managed_user(actor.uid)
        if payload.new_password != payload.confirm_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="两次输入的新密码不一致")
        if payload.current_password == payload.new_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与旧密码相同")
        if not self.password_service.verify_password(payload.current_password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="旧密码不正确")

        await self.users.update_password(user, self.password_service.hash_password(payload.new_password))
        revoked = await self.sessions.revoke_all_for_user(user.id)
        await self.audits.create(
            actor_id=user.id,
            action="password_change",
            target=f"user:{user.uid}",
            detail=json.dumps({"revoked_sessions": revoked}, ensure_ascii=False),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return MessageResponse(message="密码已更新，请重新登录。")

    async def update_preferences(
        self,
        request: Request,
        actor: User,
        payload: UserPreferencesUpdateRequest,
    ) -> UserPreferencesResponse:
        user = await self._get_managed_user(actor.uid)
        timezone = _normalize_timezone(payload.timezone)
        user = await self.users.update_timezone(user, timezone)
        await self.audits.create(
            actor_id=user.id,
            action="preferences_update",
            target=f"user:{user.uid}",
            detail=json.dumps({"timezone": timezone}, ensure_ascii=False),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return UserPreferencesResponse(timezone=user.timezone)

    async def list_login_activity(self, actor: User, limit: int = 50) -> list[LoginActivityEntry]:
        logs = await self.audits.list_login_events(actor_id=actor.id, identifier=actor.email, limit=limit)
        activity: list[LoginActivityEntry] = []
        for item in logs:
            detail = _parse_detail(item.detail)
            activity.append(
                LoginActivityEntry(
                    id=item.id,
                    method=str(detail.get("method") or "密码登录"),
                    result=str(detail.get("result") or "success"),
                    device=str(detail.get("device_fingerprint") or item.user_agent or "Unknown device"),
                    ip=item.ip,
                    time=item.created_at,
                    identifier=str(detail.get("identifier") or item.target),
                )
            )
        return activity

    def get_avatar_file(self, uid: str, filename: str) -> FileResponse:
        avatar_path = (self.config.root_dir / "data" / "avatar" / uid / filename).resolve()
        avatar_root = (self.config.root_dir / "data" / "avatar" / uid).resolve()
        if avatar_root not in avatar_path.parents or not avatar_path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="头像不存在")
        return FileResponse(avatar_path)

    async def _get_managed_user(self, uid: str) -> User:
        user = await self.users.get_by_uid(uid)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
        return user


def _guess_extension(content_type: str | None) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return mapping.get((content_type or "").lower(), ".png")


def _parse_detail(detail: str | None) -> dict[str, object]:
    if not detail:
        return {}

    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return {}

    if isinstance(parsed, dict):
        return parsed
    return {}


def _normalize_timezone(value: str) -> str:
    timezone = value.strip()
    if not timezone:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="时区不能为空")

    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="时区无效") from exc
    return timezone
