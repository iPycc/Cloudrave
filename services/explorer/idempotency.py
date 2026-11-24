"""Replay a completed operation when a client retries its key."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tables import IdempotencyRecord, User


def idempotency_key(request: Request) -> str | None:
    value = request.headers.get("Idempotency-Key")
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > 128 or re.fullmatch(r"[A-Za-z0-9._:-]+", value) is None:
        raise HTTPException(status_code=422, detail="Idempotency-Key 格式无效")
    return value


def request_digest(payload: BaseModel | dict) -> str:
    body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def execute_once(
    session: AsyncSession,
    request: Request,
    actor: User,
    operation: str,
    payload: BaseModel | dict,
    action: Callable[[], Awaitable[object]],
) -> object:
    key = idempotency_key(request)
    if key is None:
        return await action()

    digest = request_digest(payload)
    await session.scalar(select(User.id).where(User.id == actor.id).with_for_update())
    record = await session.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.owner_id == actor.id,
        IdempotencyRecord.operation == operation,
        IdempotencyRecord.key == key,
    ))
    if record is not None:
        if record.request_hash != digest:
            raise HTTPException(status_code=409, detail="幂等键已用于不同的请求")
        return record.response

    response = jsonable_encoder(await action())
    session.add(IdempotencyRecord(owner_id=actor.id, operation=operation, key=key,
                                  request_hash=digest, response=response))
    await session.flush()
    return response
