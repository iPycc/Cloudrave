"""Helpers for authenticated request context."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.dep import Dep
from app.models.enums import UserStatus
from app.security.auth_cookies import ACCESS_TOKEN_COOKIE
from app.security.jwt import AuthError


bearer_scheme = HTTPBearer(auto_error=False)


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
        await session_repo.touch(session_id)

    request.state.actor = user.uid
    return user
