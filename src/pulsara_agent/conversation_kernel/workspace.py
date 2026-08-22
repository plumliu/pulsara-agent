"""Exact session workspace lookup shared by narrow runtime coordinators."""

from __future__ import annotations

import asyncio

from pulsara_agent.conversation_kernel.contracts import WriterLease
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository


class SessionWorkspaceResolver:
    """Resolve and cache the immutable workspace bound to one Host session."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        writer_lease: WriterLease,
        io_owner: KernelSessionIO,
        workspace_id: str | None,
    ) -> None:
        self._repository = repository
        self._writer_lease = writer_lease
        self._io = io_owner
        self._workspace_id = workspace_id
        self._lock = asyncio.Lock()

    async def resolve(self, *, deadline: float) -> str:
        if self._workspace_id is not None:
            return self._workspace_id
        async with self._lock:
            if self._workspace_id is None:
                self._workspace_id = await self._io.run(
                    self._repository.read_session_workspace_id,
                    self._writer_lease.guard,
                    deadline_monotonic=deadline,
                )
            return self._workspace_id


__all__ = ["SessionWorkspaceResolver"]
