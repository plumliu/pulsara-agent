"""Controlled MCP fixture setup; never used by production request admission."""
import asyncio


async def finish_connection_attempts(supervisor):
    """Observe only the actual tasks already captured by this fixture."""
    tasks = tuple(supervisor._tasks.values())
    if tasks:
        await asyncio.gather(*(asyncio.shield(task) for task in tasks))
