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


async def consume_share_attempt(session, client_ip: str, share_id: str, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    # Fixed windows are persisted independently of the password transaction.
    window = int(now.timestamp()) // 60
    prefix = "security.share.verify."
    allowed = True
    for identity, limit in ((client_ip, 20), (f"{client_ip}\0{share_id}", 5)):
        key = prefix + hashlib.sha256(identity.encode()).hexdigest()
        dialect = session.get_bind().dialect.name
        values = {"key": key, "value": "{}", "updated_at": now}
        if dialect == "mysql":
            statement = mysql_insert(SystemMeta).values(**values)
            statement = statement.on_duplicate_key_update(key=statement.inserted.key)
        else:
            insert = sqlite_insert if dialect == "sqlite" else pg_insert
            statement = insert(SystemMeta).values(**values).on_conflict_do_nothing(index_elements=["key"])
        await session.execute(statement)
        record = await session.scalar(select(SystemMeta).where(SystemMeta.key == key).with_for_update())
        data = json.loads(record.value)
        count = int(data.get("count", 0)) if data.get("window") == window else 0
        allowed = allowed and count < limit
        record.value = json.dumps({"window": window, "count": min(count + 1, limit)})
        record.updated_at = now
        await session.flush()
    await session.execute(delete(SystemMeta).where(
        SystemMeta.key.like(prefix + "%"), SystemMeta.updated_at < now - timedelta(days=1)))
    return allowed
