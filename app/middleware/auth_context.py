"""Helpers for authenticated request context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import OperationalError

from app.dep import Dep
from app.models.enums import UserStatus
from app.security.auth_cookies import ACCESS_TOKEN_COOKIE
from app.security.jwt import AuthError


bearer_scheme = HTTPBearer(auto_error=False)
LOGGER = logging.getLogger(__name__)
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)


def _session_touch_due(last_seen_at: datetime | None) -> bool:
    if last_seen_at is None:
        return True
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - last_seen_at >= SESSION_TOUCH_INTERVAL


def _is_sqlite_locked(error: OperationalError) -> bool:
    return "database is locked" in str(error).lower()


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
):
    dep: Dep = request.app.state.dep
    access_token = credentials.credentials if credentials is not None else request.cookies.get(ACCESS_TOKEN_COOKIE)
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    try:
        payload = dep.token_service.parse_access_token(access_token)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    user_uid = str(payload["sub"])
    session_id = payload["sid"]
    token_version = int(payload.get("ver", 0))

    async with dep.session_scope() as session:
        user_repo = dep.get_user_repository(session)
        session_repo = dep.get_session_repository(session)
        session_record = await session_repo.get_active_by_id(session_id)
        user = await user_repo.get_by_uid(user_uid)
        if session_record is None or user is None or session_record.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked")
        if user.status != UserStatus.ACTIVE:
            await session_repo.revoke_all_for_user(user.id)
            await session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account disabled")
        if session_record.token_version != token_version:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session token rotated")
        touch_due = _session_touch_due(session_record.last_seen_at)

    if touch_due:
        try:
            async with dep.session_scope() as touch_session:
                await dep.get_session_repository(touch_session).touch(session_id)
        except OperationalError as error:
            if not _is_sqlite_locked(error):
                raise
            LOGGER.warning(
                "Skipped non-critical session heartbeat because SQLite is busy",
                extra={"session_id": session_id},
            )

    request.state.actor = user.uid
    return user
