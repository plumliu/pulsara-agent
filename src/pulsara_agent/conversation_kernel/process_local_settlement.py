"""Cancellation-safe joins for admitted process-local settlement tasks."""

from __future__ import annotations

import asyncio
from typing import TypeVar


_T = TypeVar("_T")


async def await_started_settlement(task: asyncio.Task[_T]) -> _T:
    """Join one admitted worker before surfacing caller cancellation."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            continue
        except BaseException:
            break
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


__all__ = ["await_started_settlement"]
