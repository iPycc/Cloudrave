"""Persistence for private preview annotation sidecars."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tables import PreviewAnnotation


class PreviewAnnotationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, owner_id: int, node_id: int) -> PreviewAnnotation | None:
        return await self.session.scalar(
            select(PreviewAnnotation).where(
                PreviewAnnotation.owner_id == owner_id,
                PreviewAnnotation.node_id == node_id,
            )
        )

    async def get_for_update(self, owner_id: int, node_id: int) -> PreviewAnnotation | None:
        return await self.session.scalar(
            select(PreviewAnnotation)
            .where(
                PreviewAnnotation.owner_id == owner_id,
                PreviewAnnotation.node_id == node_id,
            )
            .with_for_update()
        )

    async def create(
        self,
        *,
        owner_id: int,
        node_id: int,
        source_version: str,
        payload: list[dict],
    ) -> PreviewAnnotation:
        row = PreviewAnnotation(
            owner_id=owner_id,
            node_id=node_id,
            source_version=source_version,
            revision=1,
            payload=payload,
        )
        self.session.add(row)
        await self.session.flush()
        return row
