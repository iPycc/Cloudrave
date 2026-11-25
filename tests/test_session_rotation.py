from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.cfg import load_config
from db.base import create_schema, make_async_engine
from models.tables import User
from repositories.sessions import SessionRepository


@pytest.mark.asyncio
async def test_refresh_rotation_rejects_a_stale_token(tmp_path) -> None:
    engine = make_async_engine(load_config(tmp_path))
    await create_schema(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            user = User(uid="ROTATE01", email="rotate@example.test", username="rotate", password_hash="hash")
            db.add(user)
            await db.flush()
            record = await SessionRepository(db).create(
                user_id=user.id,
                refresh_token_hash="old",
                refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
                ip=None,
                user_agent=None,
            )
            session_id = record.id
            await db.commit()

        async with sessions() as db:
            repository = SessionRepository(db)
            expires_at = datetime.now(UTC) + timedelta(days=1)
            assert await repository.rotate_refresh_token(
                session_id,
                expected_hash="old",
                expected_version=1,
                refresh_token_hash="first",
                refresh_expires_at=expires_at,
            )
            await db.commit()

        async with sessions() as db:
            repository = SessionRepository(db)
            assert not await repository.rotate_refresh_token(
                session_id,
                expected_hash="old",
                expected_version=1,
                refresh_token_hash="second",
                refresh_expires_at=expires_at,
            )
            stored = await repository.get_active_by_id(session_id)
            assert stored is not None
            assert stored.refresh_token_hash == "first"
            assert stored.token_version == 2
    finally:
        await engine.dispose()
