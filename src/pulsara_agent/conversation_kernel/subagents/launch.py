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
from pulsara_agent.llm.runtime import ModelRuntime
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
        model_runtime: ModelRuntime,
        deadline_factory: KernelExecutionDeadlineFactory,
    ) -> None:
        self._repository = repository
        self._guard = guard
        self._io = io_owner
        self._model_runtime = model_runtime
        self._deadlines = deadline_factory

    async def prepare_launch(
        self, candidate: PreparedSubagentTaskStart
    ) -> PreparedSubagentLaunch:
        deadline = self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)
        permission, binding = await self._io.run(
            self._prepare_launch_truth,
            candidate=candidate,
            deadline_monotonic=deadline,
        )
        target = self._model_runtime.resolve_target(
            binding,
            timeout_policy=self._deadlines.policy.foreground_transport,
        )
        return PreparedSubagentLaunch(
            task_start=candidate,
            child_turn_id=stable_subagent_turn_id(
                session_id=candidate.session_id,
                task_id=candidate.task_id,
            ),
            configured_model_identity=target.model_profile.id,
            parent_permission_snapshot=permission,
        )

    def _prepare_launch_truth(self, *, candidate, deadline_monotonic):
        permission = self._repository.prepare_subagent_launch_permission(
            self._guard,
            candidate=candidate,
            deadline_monotonic=deadline_monotonic,
        )
        binding = self._repository.read_turn_model_call_binding(
            self._guard,
            turn_id=candidate.parent_turn_id,
            deadline_monotonic=deadline_monotonic,
        )
        return permission, binding


__all__ = [
    "CanonicalSubagentLaunchPreparationPort",
    "SubagentLaunchPreparationPort",
]
