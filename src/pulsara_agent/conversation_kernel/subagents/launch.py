"""Narrow Host-composed preparation port for one child launch."""

from __future__ import annotations

from typing import Protocol

from pulsara_agent.conversation_kernel.cancellation import stable_subagent_turn_id
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.subagents.contracts import (
    PreparedSubagentLaunch,
    PreparedSubagentTaskStart,
)


class SubagentLaunchPreparationPort(Protocol):
    async def prepare_launch(
        self, candidate: PreparedSubagentTaskStart
    ) -> PreparedSubagentLaunch: ...


class CanonicalSubagentLaunchPreparationPort:
    """Freeze model ownership and repository permission truth without a Host handle."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        guard: HostWriterGuard,
        io_owner: KernelSessionIO,
        configured_model_identity: str,
        deadline_factory: KernelExecutionDeadlineFactory,
    ) -> None:
        if not configured_model_identity:
            raise ValueError("configured child model identity is required")
        self._repository = repository
        self._guard = guard
        self._io = io_owner
        self._configured_model_identity = configured_model_identity
        self._deadlines = deadline_factory

    async def prepare_launch(
        self, candidate: PreparedSubagentTaskStart
    ) -> PreparedSubagentLaunch:
        permission = await self._io.run(
            self._repository.prepare_subagent_launch_permission,
            self._guard,
            candidate=candidate,
            deadline_monotonic=self._deadlines.deadline(
                KernelWatchdogOwner.FOREGROUND_CANONICAL
            ),
        )
        return PreparedSubagentLaunch(
            task_start=candidate,
            child_turn_id=stable_subagent_turn_id(
                session_id=candidate.session_id,
                task_id=candidate.task_id,
            ),
            configured_model_identity=self._configured_model_identity,
            parent_permission_snapshot=permission,
        )


__all__ = [
    "CanonicalSubagentLaunchPreparationPort",
    "SubagentLaunchPreparationPort",
]
