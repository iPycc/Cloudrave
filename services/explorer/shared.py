"""Shared-with-me services."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from models.enums import ShareAccess
from models.shared import ShareMount
from models.tables import Node, ShareRecord, User
from repositories.shared import SharedRepository
from schemas.shared import SharedCreate, SharedNode, SharedRead
from services.explorer.share import ShareService
from services.user.response import build_avatar_url


class SharedService:
    def __init__(self, *, shared: SharedRepository, source: ShareService) -> None:
        self.shared = shared
        self.source = source

    async def create(self, actor: User, payload: SharedCreate) -> SharedRead:
        share, nodes = await self._info(payload.share_id)
        self._require_other_owner(actor.id, share)
        if share.access == ShareAccess.PASSWORD and (
            payload.access_token is None or not self.source.verify_access_token(share, payload.access_token)
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Share access token required")
        if await self.shared.get_share(actor.id, share.id) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Share already mounted")

        try:
            async with self.shared.session.begin_nested():
                mount = await self.shared.create(
                    actor.id,
                    share.id,
                    grant_hash=self._grant_hash(share) if share.access == ShareAccess.PASSWORD else None,
                )
        except IntegrityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Share already mounted") from exc
        return self._read(mount, share, nodes, await self._owner(share.owner_id))

    async def list(self, actor: User) -> list[SharedRead]:
        mounts = await self.shared.list(actor.id)
        all_shares = {
            share.id: share
            for share in await self.source.shares.list_by_ids([mount.share_id for mount in mounts])
        }
        shares = {
            share_id: share
            for share_id, share in all_shares.items()
            if share.owner_id != actor.id
        }
        mounts = [mount for mount in mounts if mount.share_id in shares]
        root_ids = {
            item.node_id
            for share in shares.values()
            for item in (share.items or [])
        }
        root_ids.update(share.node_id for share in shares.values() if not share.items)
        nodes = {node.id: node for node in await self.source.nodes.list_by_ids(list(root_ids))}
        result: list[SharedRead] = []
        owners = {
            owner_id: await self._owner(owner_id)
            for owner_id in {share.owner_id for share in shares.values()}
        }
        now = datetime.now(UTC)
        for mount in mounts:
            share = shares.get(mount.share_id)
            if share is None or not share.is_active:
                result.append(self._unavailable(mount))
                continue
            expires_at = share.expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at is not None and expires_at < now:
                share.is_active = False
                result.append(self._unavailable(mount))
                continue
            try:
                self._require_grant(mount, share)
            except HTTPException:
                result.append(self._unavailable(mount))
                continue
            ids = [item.node_id for item in share.items] or [share.node_id]
            roots = [nodes[node_id] for node_id in ids if node_id in nodes and nodes[node_id].deleted_at is None]
            result.append(self._read(mount, share, roots, owners.get(share.owner_id)) if roots else self._unavailable(mount))
        return result

    async def get(self, actor: User, mount_id: int) -> SharedRead:
        return await self._resolve(actor, await self._mount(actor.id, mount_id))

    async def nodes(self, actor: User, mount_id: int, parent_id: int | None) -> list[SharedNode]:
        mount = await self._mount(actor.id, mount_id)
        share, _ = await self._info(mount.share_id)
        self._require_other_owner(actor.id, share)
        self._require_grant(mount, share)
        nodes = await self.source.list_shared_nodes(share, parent_id)
        return [self._node(node) for node in nodes]

    async def resolve_node(self, actor: User, mount_id: int, node_id: int) -> tuple[ShareRecord, Node]:
        mount = await self._mount(actor.id, mount_id)
        share, _ = await self._info(mount.share_id)
        self._require_other_owner(actor.id, share)
        self._require_grant(mount, share)
        return share, await self.source.resolve_shared_node(share, node_id)

    async def delete(self, actor: User, mount_id: int) -> None:
        await self.shared.delete(await self._mount(actor.id, mount_id))

    async def _mount(self, user_id: int, mount_id: int) -> ShareMount:
        mount = await self.shared.get(mount_id, user_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mounted share not found")
        return mount

    async def _resolve(self, actor: User, mount: ShareMount) -> SharedRead:
        try:
            share, nodes = await self._info(mount.share_id)
            self._require_other_owner(actor.id, share)
            self._require_grant(mount, share)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            return SharedRead(
                id=mount.id,
                share_id=mount.share_id,
                available=False,
                unavailable_reason="Source share is unavailable",
                detail_url=f"/api/v1/shared/{mount.id}",
                nodes_url=f"/api/v1/shared/{mount.id}/nodes",
                created_at=mount.created_at,
            )
        return self._read(mount, share, nodes, await self._owner(share.owner_id))

    @staticmethod
    def _unavailable(mount: ShareMount) -> SharedRead:
        return SharedRead(
            id=mount.id,
            share_id=mount.share_id,
            available=False,
            unavailable_reason="Source share is unavailable",
            detail_url=f"/api/v1/shared/{mount.id}",
            nodes_url=f"/api/v1/shared/{mount.id}/nodes",
            created_at=mount.created_at,
        )

    @classmethod
    def _require_grant(cls, mount: ShareMount, share: ShareRecord) -> None:
        if share.access == ShareAccess.PASSWORD and mount.grant_hash != cls._grant_hash(share):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mounted share grant is no longer valid")

    @staticmethod
    def _require_other_owner(user_id: int, share: ShareRecord) -> None:
        if share.owner_id == user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="不能将自己的分享挂载到“与我共享”",
            )

    @staticmethod
    def _grant_hash(share: ShareRecord) -> str:
        return hashlib.sha256((share.password_hash or "").encode()).hexdigest()

    async def _info(self, share_id: str) -> tuple[ShareRecord, list[Node]]:
        share = await self.source.shares.get_by_id(share_id)
        if share is not None and share.expires_at is not None and share.expires_at.tzinfo is None:
            share.expires_at = share.expires_at.replace(tzinfo=UTC)
        return await self.source.get_share_info(share_id)

    @classmethod
    def _read(
        cls,
        mount: ShareMount,
        share: ShareRecord,
        nodes: list[Node],
        owner: User | None = None,
    ) -> SharedRead:
        return SharedRead(
            id=mount.id,
            share_id=mount.share_id,
            access=share.access,
            owner_id=share.owner_id,
            owner_username=owner.username if owner is not None else None,
            owner_avatar=build_avatar_url(owner) if owner is not None else None,
            available=True,
            roots=[cls._node(node) for node in nodes],
            detail_url=f"/api/v1/shared/{mount.id}",
            nodes_url=f"/api/v1/shared/{mount.id}/nodes",
            created_at=mount.created_at,
        )

    async def _owner(self, owner_id: int) -> User | None:
        return await self.shared.session.get(User, owner_id)

    @staticmethod
    def _node(node: Node) -> SharedNode:
        return SharedNode(
            id=node.id,
            parent_id=node.parent_id,
            name=node.name,
            type=node.type.value,
            size=node.size,
        )
