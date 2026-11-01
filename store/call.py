"""Async bridge for storage adapters with synchronous SDK implementations."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any


async def call_adapter(adapter: Any, method: str, /, *args: Any, **kwargs: Any) -> Any:
    """Invoke an adapter operation without blocking the application event loop.

    Adapters may expose an optimized async variant named ``a<method>``. Legacy
    synchronous implementations are isolated in the default thread pool.
    """

    async_method = getattr(adapter, f"a{method}", None)
    if async_method is not None:
        result = async_method(*args, **kwargs)
        result = await result if inspect.isawaitable(result) else result
    else:
        sync_method = getattr(adapter, method)
        result = await asyncio.to_thread(sync_method, *args, **kwargs)
    # Apply ORM credential changes on the event-loop thread, after synchronous
    # provider IO has finished. Failures are saved by the background worker.
    on_success = getattr(adapter, "_on_call_success", None)
    if on_success is not None:
        on_success()
    return result
