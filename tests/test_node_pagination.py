from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.enums import NodeStatus, NodeType
from app.models.tables import Node
from app.repositories.nodes import NodeRepository
from app.services.explorer.file import _decode_page_cursor, _encode_page_cursor
from database import Base


def test_page_cursor_round_trip() -> None:
    cursor = _encode_page_cursor(400)
    assert _decode_page_cursor(cursor) == 400
    assert _decode_page_cursor(None) == 0


def test_invalid_page_cursor_is_rejected() -> None:
    with pytest.raises(HTTPException) as error:
        _decode_page_cursor("not-a-cursor")
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_node_pages_are_folder_first_and_filterable() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        root = Node(
            owner_id=1,
            mount_id=1,
            parent_id=None,
            type=NodeType.FOLDER,
            status=NodeStatus.ACTIVE,
            name="root",
        )
        session.add(root)
        await session.flush()
        session.add_all(
            [
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FOLDER, status=NodeStatus.ACTIVE, name="beta"),
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FOLDER, status=NodeStatus.ACTIVE, name="alpha"),
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FILE, status=NodeStatus.ACTIVE, name="z.txt", size=3),
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FILE, status=NodeStatus.ACTIVE, name="a.txt", size=1),
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FILE, status=NodeStatus.ACTIVE, name="photo.JPG", size=2),
            ]
        )
        await session.flush()

        repository = NodeRepository(session)
        first_page, first_has_more = await repository.list_children_page(
            1,
            root.id,
            limit=2,
            offset=0,
            sort="name-asc",
        )
        assert [node.name for node in first_page] == ["alpha", "beta"]
        assert first_has_more is True

        folder_page, folder_has_more = await repository.list_children_page(
            1,
            root.id,
            limit=10,
            offset=0,
            sort="name-asc",
            folders_only=True,
        )
        assert [node.name for node in folder_page] == ["alpha", "beta"]
        assert folder_has_more is False

        image_page, image_has_more = await repository.list_category_page(
            1,
            "image",
            limit=10,
            offset=0,
            sort="name-asc",
        )
        assert [node.name for node in image_page] == ["photo.JPG"]
        assert image_has_more is False

        search_results = await repository.search(1, "PHOTO", limit=20)
        assert [node.name for node in search_results] == ["photo.JPG"]

    await engine.dispose()
