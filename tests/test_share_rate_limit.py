from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from db.base import Base
from security.request_limits import consume_share_attempt
from api.v1.share import router
from web.mid.rate import share_password_rate_limit


@pytest.mark.asyncio
async def test_share_password_attempts_survive_new_sessions_and_expire():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine)
        now = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
        for index in range(6):
            async with factory.begin() as session:
                assert await consume_share_attempt(session, "192.0.2.1", "share", now=now) is (index < 5)
        async with factory.begin() as session:
            assert await consume_share_attempt(session, "192.0.2.2", "share", now=now)
            assert await consume_share_attempt(session, "192.0.2.1", "share", now=now + timedelta(minutes=1))
        for index in range(21):
            async with factory.begin() as session:
                assert await consume_share_attempt(session, "192.0.2.3", f"share-{index}", now=now) is (index < 20)
    finally:
        await engine.dispose()


def test_public_share_verification_route_enforces_attempt_limit():
    route = next(route for route in router.routes if route.path == "/share/{share_id}/verify")
    assert share_password_rate_limit in [dependency.call for dependency in route.dependant.dependencies]
