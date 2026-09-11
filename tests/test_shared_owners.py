from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.base import Base
from models.enums import NodeType, ShareAccess, UserRole
from models.tables import Node, ShareRecord, User
from models.shared import ShareMount
from repositories.nodes import NodeRepository
from repositories.shares import ShareRepository
from repositories.shared import SharedRepository
from services.explorer.shared import SharedService


@pytest.mark.asyncio
async def test_owner_summary_filters_unavailable_shares_and_isolates_users():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            now = datetime.now(UTC)
            for user_id in range(1, 5):
                session.add(User(id=user_id, uid=str(user_id), username=f"user{user_id}",
                                 email=f"user{user_id}@example.com", password_hash="test"))
            node = Node(owner_id=2, mount_id=1, type=NodeType.FILE, name="visible.txt", size=1)
            deleted = Node(owner_id=2, mount_id=1, type=NodeType.FILE, name="deleted.txt", deleted_at=now)
            session.add_all([node, deleted])
            await session.flush()
            cases = [
                ("a", 2, True, None, ShareAccess.PUBLIC, node.id, None, 1),
                ("b", 2, True, None, ShareAccess.PUBLIC, node.id, None, 1),
                ("c", 3, True, None, ShareAccess.PUBLIC, node.id, None, 1),
                ("inactive", 4, False, None, ShareAccess.PUBLIC, node.id, None, 1),
                ("expired", 4, True, now - timedelta(days=1), ShareAccess.PUBLIC, node.id, None, 1),
                ("revoked", 4, True, None, ShareAccess.PASSWORD, node.id, "old", 1),
                ("deleted", 4, True, None, ShareAccess.PUBLIC, deleted.id, None, 1),
                ("self", 1, True, None, ShareAccess.PUBLIC, node.id, None, 1),
                ("other-user", 4, True, None, ShareAccess.PUBLIC, node.id, None, 2),
            ]
            for index, (key, owner, active, expires, access, root, grant, user) in enumerate(cases):
                session.add(ShareRecord(id=key, owner_id=owner, node_id=root, access=access,
                                        password_hash="new", is_active=active, expires_at=expires, items=[]))
                session.add(ShareMount(user_id=user, share_id=key, grant_hash=grant,
                                       created_at=now + timedelta(seconds=index)))
            await session.flush()
            service = SharedService(shared=SharedRepository(session), source=SimpleNamespace(
                shares=ShareRepository(session), nodes=NodeRepository(session)))
            actor = SimpleNamespace(id=1, role=UserRole.USER)
            queries = []
            def record(conn, cursor, statement, parameters, context, executemany):
                queries.append(statement)
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            owners = await service.owners(actor)
            event.remove(engine.sync_engine, "before_cursor_execute", record)
            assert [(owner.id, owner.name, owner.share_count) for owner in owners] == [
                (3, "user3", 1), (2, "user2", 2)]
            assert sum("FROM users" in query for query in queries) == 1
            summary = await service.summary(actor)
            assert (summary.owner_count, summary.share_count) == (2, 3)
            assert await service.owners(SimpleNamespace(id=4, role=UserRole.USER)) == []
            with pytest.raises(HTTPException) as error:
                await service.owners(SimpleNamespace(id=1, role=UserRole.GUEST))
            assert error.value.status_code == 403
    finally:
        await engine.dispose()
