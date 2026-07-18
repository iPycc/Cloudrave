"""Bounded async worker helpers for explorer operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar


T = TypeVar("T")
R = TypeVar("R")


async def map_limited(items: Iterable[T], worker: Callable[[T], Awaitable[None]], *, limit: int = 8) -> None:
    """Process an iterable with a fixed number of tasks and natural backpressure."""

    iterator = iter(items)
    lock = asyncio.Lock()

    async def run() -> None:
        while True:
            async with lock:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
            await worker(item)

    async with asyncio.TaskGroup() as group:
        for _ in range(max(1, limit)):
            group.create_task(run())


async def collect_limited(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    limit: int = 8,
) -> list[R]:
    """Collect results while keeping only a fixed worker set active."""

    iterator = iter(items)
    lock = asyncio.Lock()
    results: list[R] = []

    async def run() -> None:
        while True:
            async with lock:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
            result = await worker(item)
            async with lock:
                results.append(result)

    async with asyncio.TaskGroup() as group:
        for _ in range(max(1, limit)):
            group.create_task(run())
    return results
