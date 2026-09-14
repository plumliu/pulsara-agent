"""Narrow process-local subagent Runtime port shared by foreground owners."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from pulsara_agent.conversation_kernel.cold_epoch import SubagentInitialSeed
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenSubagentResultPublicFact,
    SubagentProfileKind,
)
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.model_input.contracts import (
    ContextSourceAbsentFact,
    ContextSourceCandidate,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)


class SubagentRuntimePort(Protocol):
    def profile_kind(self, *, task_id: str) -> SubagentProfileKind: ...

    def build_initial_seed(
        self,
        *,
        task_id: str,
        dispatch_read: FrozenCanonicalProviderDispatchRead,
    ) -> SubagentInitialSeed: ...

    def initial_context_sources(
        self, *, task_id: str
    ) -> tuple[ContextSourceCandidate | ContextSourceAbsentFact, ...]: ...

    async def consume_mailbox_safe_point(self, task_id: str) -> bool: ...

    async def open_root_completion_delivery(self, turn_id: str) -> None: ...

    async def seal_root_completion_delivery(self, turn_id: str) -> int: ...

    async def root_completion_followup_possible(self, turn_id: str) -> bool: ...

    async def settle_root_completion_delivery(
        self, turn_id: str, *, turn_completed: bool
    ) -> None: ...

    async def close_root_completion_delivery(self, turn_id: str) -> None: ...

    async def snapshot_pending_root_completions(
        self, turn_id: str
    ) -> tuple[str, ...]: ...

    async def retire_root_completion(self, task_id: str) -> bool: ...

    async def notify_root_input_activity(self) -> None: ...

    async def prepare_inferred_completion(
        self,
        *,
        task_id: str,
        entry_id: str,
        public_text: str,
        model_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> "PreparedInferredSubagentCompletion | None": ...

    async def prepare_explicit_completion(
        self,
        *,
        task_id: str,
        result_entry_id: str,
        arguments: Mapping[str, object],
        last_assistant_message: str | None,
        model_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
    ) -> "PreparedExplicitSubagentCompletion | None": ...

    async def finish_completion(self, permit: object, *, committed: bool) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparedInferredSubagentCompletion:
    permit: object = field(repr=False, compare=False)
    result: FrozenSubagentResultPublicFact | None


@dataclass(frozen=True, slots=True)
class PreparedExplicitSubagentCompletion:
    permit: object = field(repr=False, compare=False)
    result: FrozenSubagentResultPublicFact | None
    tool_result: KernelToolResult


__all__ = [
    "PreparedExplicitSubagentCompletion",
    "PreparedInferredSubagentCompletion",
    "SubagentRuntimePort",
]
