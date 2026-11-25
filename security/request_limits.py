"""Database-backed attempt limits shared by application processes."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from models.tables import SystemMeta
from db.base import session_scope


async def consume_share_attempt(session, client_ip: str, share_id: str | None, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    # Fixed windows are persisted independently of the password transaction.
    window = int(now.timestamp()) // 60
    prefix = "security.share.verify."
    identities = [(client_ip, 20)]
    if share_id is not None:
        identities.append((f"{client_ip}\0{share_id}", 5))
    for identity, limit in identities:
        key = prefix + hashlib.sha256(identity.encode()).hexdigest()
        dialect = session.get_bind().dialect.name
        values = {"key": key, "value": "{}", "updated_at": now}
        if dialect == "mysql":
            statement = mysql_insert(SystemMeta).values(**values)
            statement = statement.on_duplicate_key_update(key=statement.inserted.key)
        else:
            insert = sqlite_insert if dialect == "sqlite" else pg_insert
            statement = insert(SystemMeta).values(**values).on_conflict_do_update(index_elements=["key"], set_={"key": key})
        await session.execute(statement)
        record = await session.scalar(select(SystemMeta).where(SystemMeta.key == key).with_for_update()
                                      .execution_options(populate_existing=True))
        data = json.loads(record.value)
        effective_window = max(window, int(data.get("window", window)))
        count = int(data.get("count", 0)) if data.get("window") == effective_window else 0
        record.value = json.dumps({"window": effective_window, "count": min(count + 1, limit)})
        if effective_window == window:
            record.updated_at = max(now, record.updated_at.replace(tzinfo=UTC))
        await session.flush()
        if count >= limit:
            return False
    return True


async def cleanup_share_attempts(session_factory) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=1)
    async with session_scope(session_factory) as session:
        keys = list(await session.scalars(select(SystemMeta.key).where(
            SystemMeta.key.like("security.share.verify.%"), SystemMeta.updated_at < cutoff,
        ).order_by(SystemMeta.key).limit(100)))
    # Never hold one counter lock while waiting for another verification counter.
    for key in keys:
        async with session_scope(session_factory) as session:
            await session.execute(delete(SystemMeta).where(SystemMeta.key == key, SystemMeta.updated_at < cutoff))
