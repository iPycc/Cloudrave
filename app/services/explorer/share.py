"""Explorer share services."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NodeType, ShareAccess
from app.models.tables import Node, ShareRecord, User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.nodes import NodeRepository
from app.repositories.shares import ShareRepository
from app.schemas.share import ShareCreateRequest, ShareRead, ShareVerifyRequest


class ShareService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        shares: ShareRepository,
        nodes: NodeRepository,
        audits: AuditLogRepository,
        share_signing_key: bytes,
    ) -> None:
        self.session = session
        self.shares = shares
        self.nodes = nodes
        self.audits = audits
        self.share_signing_key = share_signing_key

    async def create_share(self, request: Request, actor: User, payload: ShareCreateRequest) -> ShareRead:
        node = await self.nodes.get_by_id(payload.node_id)
        if node is None or node.owner_id != actor.id or node.deleted_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")

        password_hash = None
        if payload.access == ShareAccess.PASSWORD and payload.password:
            password_hash = self._hash_password(payload.password)

        expires_at = None
        if payload.expires_in_hours is not None:
            expires_at = datetime.now(UTC) + timedelta(hours=payload.expires_in_hours)

        share = await self.shares.create(
            owner_id=actor.id,
            node_id=node.id,
            access=payload.access,
            password_hash=password_hash,
            expires_at=expires_at,
            max_downloads=payload.max_downloads,
        )
        await self.audits.create(
            actor_id=actor.id,
            action="share.create",
            target=f"share:{share.id}",
            detail=f"node:{node.id}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return ShareRead.model_validate(share)

    async def list_shares(self, actor: User) -> list[ShareRead]:
        shares = await self.shares.list_for_owner(actor.id)
        return [ShareRead.model_validate(share) for share in shares]

    async def revoke_share(self, request: Request, actor: User, share_id: str) -> None:
        share = await self.shares.get_by_id(share_id)
        if share is None or share.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
        await self.shares.delete(share)
        await self.audits.create(
            actor_id=actor.id,
            action="share.revoke",
            target=f"share:{share_id}",
            detail=f"node:{share.node_id}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )

    async def get_share_info(self, share_id: str) -> tuple[ShareRecord, Node]:
        share = await self._require_valid_share(share_id)
        node = await self.nodes.get_by_id(share.node_id)
        if node is None or node.deleted_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
        return share, node

    async def verify_password(self, share_id: str, payload: ShareVerifyRequest) -> str:
        share = await self._require_valid_share(share_id)
        if share.access != ShareAccess.PASSWORD:
            return self._issue_access_token(share)
        if payload.password is None or not self._verify_password(payload.password, share.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid share password")
        return self._issue_access_token(share)

    async def record_view(self, share: ShareRecord) -> None:
        await self.shares.increment_view(share)

    async def record_download(self, share: ShareRecord) -> None:
        await self.shares.increment_download(share)

    async def _require_valid_share(self, share_id: str) -> ShareRecord:
        share = await self.shares.get_by_id(share_id)
        if share is None or not share.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
        if share.expires_at is not None and share.expires_at < datetime.now(UTC):
            share.is_active = False
            await self.session.flush()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share expired")
        return share

    def _hash_password(self, password: str) -> str:
        salt = secrets.token_hex(16)
        digest = hmac.new(self.share_signing_key, f"{salt}:{password}".encode(), hashlib.sha256).hexdigest()
        return f"{salt}:${digest}"

    def _verify_password(self, password: str, password_hash: str | None) -> bool:
        if password_hash is None:
            return False
        salt, _, digest = password_hash.partition("$")
        expected = hmac.new(self.share_signing_key, f"{salt}:{password}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, expected)

    def _issue_access_token(self, share: ShareRecord) -> str:
        payload = f"{share.id}:{share.node_id}:{int(datetime.now(UTC).timestamp())}"
        signature = hmac.new(self.share_signing_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}:{signature}"

    def verify_access_token(self, share: ShareRecord, token: str) -> bool:
        expected = self._issue_access_token(share)
        return hmac.compare_digest(expected, token)
