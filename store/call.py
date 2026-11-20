"""Async bridge for storage adapters with synchronous SDK implementations."""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import Executor
from contextvars import copy_context
from functools import partial
from typing import Any


async def call_adapter(
    adapter: Any, method: str, /, *args: Any, executor: Executor | None = None, **kwargs: Any,
) -> Any:
    """Invoke an adapter operation without blocking the application event loop.

    Adapters may expose an optimized async variant named ``a<method>``. Legacy
    synchronous implementations use the default thread pool. An explicit
    executor runs the synchronous method there, including SDKs whose async
    variants wrap the same method in the default pool.
    """

    async_method = getattr(adapter, f"a{method}", None) if executor is None else None
    if async_method is not None:
        result = async_method(*args, **kwargs)
        result = await result if inspect.isawaitable(result) else result
    else:
        sync_method = getattr(adapter, method)
        if executor is None:
            result = await asyncio.to_thread(sync_method, *args, **kwargs)
        else:
            operation = partial(sync_method, *args, **kwargs)
            result = await asyncio.get_running_loop().run_in_executor(executor, copy_context().run, operation)
    # Apply ORM credential changes on the event-loop thread, after synchronous
    # provider IO has finished. Failures are saved by the background worker.
    on_success = getattr(adapter, "_on_call_success", None)
    if on_success is not None:
        on_success()
    return result
