"""Host-scoped, process-local memory citation and anti-echo capability.

The table intentionally has no persistence or rehydrate path.  A historical
ToolResult remains provider-visible after a Host restart, but only a result
whose exact execution binding was observed by this Host can receive an opaque
``tool:N`` handle.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenMemoryCitationHandle,
    FrozenModelCallMemoryContext,
    FrozenModelVisibleMemoryProvenance,
    MemoryCitationEvidenceKind,
    MemoryCitationVisibility,
    ModelVisibleMemoryProvenanceDisposition,
    MemoryUsePolicy,
    PreparedMemoryToolResultReference,
)
from pulsara_agent.model_input.contracts import (
    ContextSourceCandidate,
    FrozenCanonicalCompileSnapshot,
    FrozenProviderInputItemKind,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope


@dataclass(frozen=True, slots=True)
class _RegisteredToolResult:
    result_id: str
    result_entry_sequence: int
    visibility: MemoryCitationVisibility
    evidence_kind: MemoryCitationEvidenceKind
    execution_binding_fingerprint: str
    epoch_nonce: str


class ProcessLocalMemoryCallContextOwner:
    """Issue exact-call snapshots from authenticated same-Host results."""

    def __init__(self, *, session_id: str) -> None:
        self._session_id = session_id
        self._lock = RLock()
        self._results: dict[tuple[str, str | None, str], _RegisteredToolResult] = {}
        self._handles: dict[tuple[str, str | None, str, str], str] = {}
        self._next_ordinal: dict[tuple[str, str | None, str], int] = {}

    def register_result(
        self,
        *,
        scope: ProviderInputContinuityScope,
        epoch_nonce: str,
        result_id: str,
        result_entry_sequence: int,
        visibility: MemoryCitationVisibility,
        evidence_kind: MemoryCitationEvidenceKind,
        execution_binding_fingerprint: str,
    ) -> None:
        self._require_scope(scope)
        if not epoch_nonce or not result_id or result_entry_sequence < 0:
            raise ValueError("memory citation registration is incomplete")
        value = _RegisteredToolResult(
            result_id,
            result_entry_sequence,
            visibility,
            evidence_kind,
            execution_binding_fingerprint,
            epoch_nonce,
        )
        key = (scope.scope_kind.value, scope.scope_subagent_task_id, result_id)
        with self._lock:
            current = self._results.get(key)
            if current is not None and current != value:
                raise RuntimeError("memory citation result identity drifted")
            self._results[key] = value

    def freeze_call(
        self,
        *,
        scope: ProviderInputContinuityScope,
        epoch_nonce: str,
        canonical_facts: FrozenCanonicalCompileSnapshot,
        sources: tuple[ContextSourceCandidate, ...],
        memory_use_policy: MemoryUsePolicy = MemoryUsePolicy.ENABLED,
    ) -> tuple[FrozenModelCallMemoryContext, tuple[tuple[str, str], ...]]:
        self._require_scope(scope)
        identity = canonical_facts.canonical_input.identity
        if (
            identity.session_id != scope.session_id
            or identity.conversation_scope_kind is not scope.scope_kind
            or identity.scope_subagent_task_id != scope.scope_subagent_task_id
        ):
            raise ValueError("memory model-call scope does not exact-join")
        epoch_key = (scope.scope_kind.value, scope.scope_subagent_task_id, epoch_nonce)
        citations: list[FrozenMemoryCitationHandle] = []
        handle_pairs: list[tuple[str, str]] = []
        visible_ids: list[str] = []
        seen_memory: set[str] = set()
        with self._lock:
            for source in sorted(sources, key=lambda item: item.placement_ordinal):
                for fact_id in source.model_visible_memory_fact_ids:
                    if fact_id not in seen_memory:
                        seen_memory.add(fact_id)
                        visible_ids.append(fact_id)
            for item in canonical_facts.canonical_input.items:
                if item.item_kind not in {
                    FrozenProviderInputItemKind.TOOL_RESULT,
                    FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
                }:
                    continue
                metadata = item.tool_result_context
                if metadata is None or item.source_entry_sequence is None:
                    continue
                for fact_id in metadata.model_visible_memory_fact_ids:
                    if fact_id not in seen_memory:
                        seen_memory.add(fact_id)
                        visible_ids.append(fact_id)
                key = (
                    scope.scope_kind.value,
                    scope.scope_subagent_task_id,
                    metadata.result_id,
                )
                registered = self._results.get(key)
                if (
                    registered is None
                    or registered.epoch_nonce != epoch_nonce
                    or registered.result_entry_sequence != item.source_entry_sequence
                    or item.source_entry_sequence
                    > identity.provider_input_through_sequence
                ):
                    continue
                handle_key = (*epoch_key, metadata.result_id)
                handle = self._handles.get(handle_key)
                if handle is None:
                    ordinal = self._next_ordinal.get(epoch_key, 1)
                    handle = f"tool:{ordinal}"
                    self._next_ordinal[epoch_key] = ordinal + 1
                    self._handles[handle_key] = handle
                reference = PreparedMemoryToolResultReference(
                    origin_session_id=self._session_id,
                    tool_result_id=metadata.result_id,
                    ordinal=len(citations),
                    evidence_kind=registered.evidence_kind,
                    citation_visibility=registered.visibility,
                )
                citations.append(FrozenMemoryCitationHandle(handle, reference))
                handle_pairs.append((metadata.result_id, handle))
        try:
            visible = FrozenModelVisibleMemoryProvenance(
                ModelVisibleMemoryProvenanceDisposition.COMPLETE,
                tuple(visible_ids),
            )
        except ValueError:
            visible = FrozenModelVisibleMemoryProvenance(
                ModelVisibleMemoryProvenanceDisposition.OVERFLOW,
                (),
            )
        return (
            FrozenModelCallMemoryContext(
                visible,
                tuple(citations),
                memory_use_policy,
            ),
            tuple(handle_pairs),
        )

    def discard_scope(self, scope: ProviderInputContinuityScope) -> None:
        self._require_scope(scope)
        prefix = (scope.scope_kind.value, scope.scope_subagent_task_id)
        with self._lock:
            for mapping in (self._results, self._handles, self._next_ordinal):
                for key in tuple(mapping):
                    if key[:2] == prefix:
                        mapping.pop(key, None)

    def _require_scope(self, scope: ProviderInputContinuityScope) -> None:
        if scope.session_id != self._session_id:
            raise ValueError("memory call context belongs to another session")


__all__ = ["ProcessLocalMemoryCallContextOwner"]
