"""Narrow Host-composed preparation port for one child launch."""

from __future__ import annotations

from typing import Callable, Protocol

from pulsara_agent.conversation_kernel.cancellation import stable_subagent_turn_id
from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.llm.model_catalog import ReasoningSelectableControls
from pulsara_agent.conversation_kernel.subagents.model_target import FrozenSubagentModelTarget
from pulsara_agent.conversation_kernel.subagents.contracts import (
    PreparedSubagentLaunch,
    PreparedSubagentTaskStart,
    FrozenSubagentParentContextCallSubject,
)


class SubagentLaunchPreparationPort(Protocol):
    def inherit_target(self, subject: FrozenSubagentParentContextCallSubject, binding: ModelCallBinding) -> FrozenSubagentModelTarget: ...
    def list_models(self) -> tuple[dict[str, object], ...]: ...
    def freeze_target(self, binding: ModelCallBinding, *, use_default: bool) -> tuple[ModelCallBinding, FrozenSubagentModelTarget]: ...

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
        inherit_parent_target: Callable[[FrozenSubagentParentContextCallSubject, ModelCallBinding], FrozenSubagentModelTarget],
    ) -> None:
        self._repository = repository
        self._guard = guard
        self._io = io_owner
        self._model_runtime = model_runtime
        self._deadlines = deadline_factory
        self.inherit_target = inherit_parent_target

    def freeze_target(self, binding: ModelCallBinding, *, use_default: bool) -> tuple[ModelCallBinding, FrozenSubagentModelTarget]:
        snapshot = self._model_runtime.freeze_resolution_snapshot()
        if use_default:
            binding, _resolved, _changed = snapshot.reconcile(binding)
        else:
            snapshot.validate(binding)
        target = self._model_runtime.resolve_target(
            binding, timeout_policy=self._deadlines.policy.foreground_transport
        )
        return binding, FrozenSubagentModelTarget.freeze(target.fact, target.contract.reasoning)

    def list_models(self) -> tuple[dict[str, object], ...]:
        snapshot = self._model_runtime.freeze_resolution_snapshot()
        options: list[dict[str, object]] = []
        for connection_id, resolved in sorted(snapshot.resolved.items(), key=lambda item: item[0].value):
            reasoning = resolved.target.reasoning
            choices: dict[str, object] = {}
            if isinstance(reasoning, ReasoningSelectableControls):
                if reasoning.effort is not None:
                    choices["effort"] = list(reasoning.effort.values)
                if reasoning.toggle is not None:
                    choices["toggle"] = [False, True]
                if reasoning.budget is not None and reasoning.budget.closed:
                    choices["budget_tokens"] = [reasoning.budget.minimum_tokens, reasoning.budget.maximum_tokens]
            options.append({
                "connection_id": connection_id.value,
                "model_id": resolved.target.key.model_id,
                "route_id": resolved.target.key.route_id,
                "wire_api": resolved.target.key.wire_api.value,
                "reasoning": choices,
            })
        return tuple(options)

    async def prepare_launch(
        self, candidate: PreparedSubagentTaskStart
    ) -> PreparedSubagentLaunch:
        deadline = self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL)
        permission, binding, accepted_fact = await self._io.run(
            self._prepare_launch_truth,
            candidate=candidate,
            deadline_monotonic=deadline,
        )
        target = self._model_runtime.resolve_target(
            binding, timeout_policy=self._deadlines.policy.foreground_transport
        )
        if FrozenSubagentModelTarget.freeze(target.fact, target.contract.reasoning) != accepted_fact:
            raise ValueError("accepted subagent model target changed before launch")
        return PreparedSubagentLaunch(
            task_start=candidate,
            child_turn_id=stable_subagent_turn_id(
                session_id=candidate.session_id,
                task_id=candidate.task_id,
            ),
            configured_model_identity=target.model_profile.id,
            accepted_model_target_fact=accepted_fact,
            parent_permission_snapshot=permission,
        )

    def _prepare_launch_truth(self, *, candidate, deadline_monotonic):
        permission = self._repository.prepare_subagent_launch_permission(
            self._guard,
            candidate=candidate,
            deadline_monotonic=deadline_monotonic,
        )
        binding, target_fact = self._repository.read_subagent_task_model_target(
            self._guard,
            task_id=candidate.task_id,
            deadline_monotonic=deadline_monotonic,
        )
        return permission, binding, target_fact


__all__ = [
    "CanonicalSubagentLaunchPreparationPort",
    "SubagentLaunchPreparationPort",
]
