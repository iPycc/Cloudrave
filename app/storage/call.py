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
        return await result if inspect.isawaitable(result) else result

    sync_method = getattr(adapter, method)
    return await asyncio.to_thread(sync_method, *args, **kwargs)
