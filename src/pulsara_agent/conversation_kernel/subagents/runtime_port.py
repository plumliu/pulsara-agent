"""Narrow process-local subagent Runtime port shared by foreground owners."""

from __future__ import annotations

from typing import Mapping, Protocol

from pulsara_agent.conversation_kernel.cold_epoch import SubagentInitialSeed
from pulsara_agent.conversation_kernel.subagents.contracts import (
    FrozenSubagentResultPublicFact,
    SubagentProfileKind,
)
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
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

    async def prepare_inferred_completion(
        self, *, task_id: str, entry_id: str, public_text: str
    ) -> tuple[object, FrozenSubagentResultPublicFact] | None: ...

    async def prepare_explicit_completion(
        self,
        *,
        task_id: str,
        result_entry_id: str,
        arguments: Mapping[str, object],
    ) -> tuple[object, FrozenSubagentResultPublicFact, KernelToolResult] | None: ...

    async def finish_completion(self, permit: object, *, committed: bool) -> None: ...


__all__ = ["SubagentRuntimePort"]
