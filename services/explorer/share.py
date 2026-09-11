"""Explorer share services."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import NodeType, ShareAccess
from models.tables import Node, ShareRecord, User
from repositories.audit_logs import AuditLogRepository
from repositories.nodes import NodeRepository
from repositories.shares import ShareRepository
from schemas.share import ShareCreateRequest, ShareRead, ShareVerifyRequest
from security.capabilities import Capability, require_capability


class ShareService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        shares: ShareRepository,
        nodes: NodeRepository,
        audits: AuditLogRepository,
        share_signing_key: bytes,
        access_token_ttl: timedelta = timedelta(hours=12),
    ) -> None:
        self.session = session
        self.shares = shares
        self.nodes = nodes
        self.audits = audits
        self.share_signing_key = share_signing_key
        self.access_token_ttl = access_token_ttl

    @staticmethod
    def _serialize_share(share: ShareRecord, node: Node | None) -> ShareRead:
        return ShareRead.model_validate(share).model_copy(
            update={
                "node_name": node.name if node else None,
                "node_type": node.type.value if node else None,
                "node_size": node.size if node else None,
            }
        )

    async def create_share(self, request: Request, actor: User, payload: ShareCreateRequest) -> ShareRead:
        require_capability(actor, Capability.SHARE_MANAGE)
        requested_ids = list(dict.fromkeys(payload.node_ids or ([payload.node_id] if payload.node_id is not None else [])))
        if not requested_ids:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="At least one node is required")
        nodes = await self.nodes.list_by_ids(requested_ids)
        nodes_by_id = {node.id: node for node in nodes}
        if len(nodes) != len(requested_ids) or any(
            node.owner_id != actor.id or node.deleted_at is not None for node in nodes
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
        nodes = [nodes_by_id[node_id] for node_id in requested_ids]
        node = nodes[0]

        password_hash = None
        if payload.access == ShareAccess.PASSWORD and payload.password:
            password_hash = self._hash_password(payload.password)

        expires_at = None
        if payload.expires_in_hours is not None:
            expires_at = datetime.now(UTC) + timedelta(hours=payload.expires_in_hours)

        share = await self.shares.create(
            owner_id=actor.id,
            node_ids=[item.id for item in nodes],
            access=payload.access,
            password_hash=password_hash,
            expires_at=expires_at,
            max_downloads=payload.max_downloads,
        )
        await self.audits.create(
            actor_id=actor.id,
            action="share.create",
            target=f"share:{share.id}",
            detail=f"nodes:{','.join(str(item.id) for item in nodes)}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return self._serialize_share(share, node).model_copy(update={"item_count": len(nodes)})

    async def list_shares(self, actor: User) -> list[ShareRead]:
        require_capability(actor, Capability.SHARE_MANAGE)
        shares = await self.shares.list_for_owner(actor.id)
        return [
            self._serialize_share(share, share.node).model_copy(update={"item_count": len(share.items) or 1})
            for share in shares
        ]

    async def revoke_share(self, request: Request, actor: User, share_id: str) -> None:
        require_capability(actor, Capability.SHARE_MANAGE)
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

    async def get_share_info(self, share_id: str) -> tuple[ShareRecord, list[Node]]:
        share = await self._require_valid_share(share_id)
        node_ids = await self.shares.list_root_node_ids(share)
        nodes = await self.nodes.list_by_ids(node_ids)
        nodes_by_id = {node.id: node for node in nodes if node.deleted_at is None}
        ordered = [nodes_by_id[node_id] for node_id in node_ids if node_id in nodes_by_id]
        if not ordered:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
        return share, ordered

    async def list_shared_nodes(self, share: ShareRecord, parent_id: int | None) -> list[Node]:
        roots = (await self.get_share_info(share.id))[1]
        if parent_id is None:
            return roots
        parent = await self.resolve_shared_node(share, parent_id)
        if parent.type != NodeType.FOLDER:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Node is not a folder")
        return await self.nodes.list_children(parent.mount_id, parent.id)

    async def resolve_shared_node(self, share: ShareRecord, node_id: int | None) -> Node:
        roots = (await self.get_share_info(share.id))[1]
        target_id = node_id if node_id is not None else roots[0].id
        node = await self.nodes.get_by_id(target_id)
        if node is None or node.deleted_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shared node not found")

        root_ids = {root.id for root in roots}
        cursor: Node | None = node
        visited: set[int] = set()
        while cursor is not None and cursor.id not in visited:
            if cursor.id in root_ids:
                return node
            visited.add(cursor.id)
            cursor = await self.nodes.get_by_id(cursor.parent_id) if cursor.parent_id is not None else None
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shared node not found")

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
        if not await self.shares.increment_download(share):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Share download limit reached")

    async def _require_valid_share(self, share_id: str) -> ShareRecord:
        share = await self.shares.get_by_id(share_id)
        if share is None or not share.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
        expires_at = self._utc_expiry(share.expires_at)
        if expires_at is not None and expires_at < datetime.now(UTC):
            share.is_active = False
            await self.session.flush()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share expired")
        return share

    def _hash_password(self, password: str) -> str:
        salt = secrets.token_hex(16)
        digest = hmac.new(self.share_signing_key, f"{salt}:{password}".encode(), hashlib.sha256).hexdigest()
        return f"{salt}${digest}"

    def _verify_password(self, password: str, password_hash: str | None) -> bool:
        if password_hash is None:
            return False
        salt, _, digest = password_hash.partition("$")
        expected = hmac.new(self.share_signing_key, f"{salt}:{password}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, expected)

    def _issue_access_token(self, share: ShareRecord) -> str:
        grant = self._access_grant(share)
        payload = f"{share.id}:{share.node_id}:{int(datetime.now(UTC).timestamp())}:{grant}"
        signature = hmac.new(self.share_signing_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}:{signature}"

    def verify_access_token(self, share: ShareRecord, token: str) -> bool:
        parts = token.split(":")
        if len(parts) != 5:
            return False
        share_id, node_id_str, ts_str, grant, provided_sig = parts
        if share_id != share.id or node_id_str != str(share.node_id):
            return False
        if not hmac.compare_digest(grant, self._access_grant(share)):
            return False
        try:
            ts = int(ts_str)
        except ValueError:
            return False
        issued_at = datetime.fromtimestamp(ts, UTC)
        now = datetime.now(UTC)
        if issued_at > now + timedelta(minutes=5) or now - issued_at > self.access_token_ttl:
            return False
        expires_at = self._utc_expiry(share.expires_at)
        if expires_at is not None and issued_at > expires_at:
            return False
        payload = f"{share_id}:{node_id_str}:{ts_str}:{grant}"
        expected_sig = hmac.new(self.share_signing_key, payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided_sig, expected_sig)

    @staticmethod
    def _access_grant(share: ShareRecord) -> str:
        value = f"{share.access.value}:{share.password_hash or ''}"
        return hashlib.sha256(value.encode()).hexdigest()[:24]

    @staticmethod
    def _utc_expiry(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
