"""ContextSource candidate selection, lifecycle caching, and validation."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Protocol

from pulsara_agent.primitives.context import (
    ContextCandidateCollectionDecisionFact,
    ContextCandidateCollectionPolicyFact,
    ContextCandidateInvalidationFact,
    ContextCandidateLifecycleDecisionFact,
    ContextCandidateLifecycleKeyFact,
    ContextCandidateSourceSelectionFact,
    ContextFactSnapshotFact,
    ContextSectionCandidate,
    PreparedContextCandidateEntryFact,
    PreparedContextCandidateSet,
    context_fingerprint,
)
from pulsara_agent.primitives.long_horizon import SubagentGraphSemanticSourceFact
from pulsara_agent.primitives.context_source import (
    ArtifactContextSourceContentSemanticFact,
    context_source_payload_content,
)
from pulsara_agent.runtime.context_input.sources.render import (
    render_context_source_candidate,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.subagent.facts import SubagentGraphState


class ContextLifecycleCachePort(Protocol):
    def get(
        self, key: ContextCandidateLifecycleKeyFact
    ) -> ContextSectionCandidate | None: ...

    def put(
        self,
        key: ContextCandidateLifecycleKeyFact,
        candidate: ContextSectionCandidate,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ContextLifecycleCacheWriteCandidate:
    key: ContextCandidateLifecycleKeyFact
    candidate: ContextSectionCandidate


@dataclass(frozen=True, slots=True)
class PreparedContextCandidateCollection:
    prepared: PreparedContextCandidateSet
    cache_writes: tuple[ContextLifecycleCacheWriteCandidate, ...]
    operational_diagnostics: tuple["ContextLifecycleCacheOperationalDiagnostic", ...]


@dataclass(frozen=True, slots=True)
class ContextLifecycleCacheOperationalDiagnostic:
    source_instance_id: str
    operation: str
    error: BaseException


class InMemoryContextLifecycleCache:
    """Session-owned optimization cache; never a source of durable truth."""

    def __init__(
        self,
        *,
        max_entries: int = 256,
        max_chars: int = 512_000,
    ) -> None:
        if max_entries < 1 or max_chars < 1:
            raise ValueError("context lifecycle cache bounds must be positive")
        self._lock = RLock()
        self._max_entries = max_entries
        self._max_chars = max_chars
        self._values: OrderedDict[
            ContextCandidateLifecycleKeyFact, ContextSectionCandidate
        ] = OrderedDict()
        self._total_chars = 0
        self._eviction_count = 0
        self._skipped_oversize_entries = 0

    def get(
        self, key: ContextCandidateLifecycleKeyFact
    ) -> ContextSectionCandidate | None:
        with self._lock:
            candidate = self._values.get(key)
            if candidate is not None:
                self._values.move_to_end(key)
            return candidate

    def put(
        self,
        key: ContextCandidateLifecycleKeyFact,
        candidate: ContextSectionCandidate,
    ) -> None:
        with self._lock:
            candidate_chars = _candidate_cache_chars(candidate)
            if candidate_chars > self._max_chars:
                self._skipped_oversize_entries += 1
                return
            existing = self._values.pop(key, None)
            if existing is not None:
                self._total_chars -= _candidate_cache_chars(existing)
            self._values[key] = candidate
            self._total_chars += candidate_chars
            self._values.move_to_end(key)
            while (
                len(self._values) > self._max_entries
                or self._total_chars > self._max_chars
            ):
                _evicted_key, evicted = self._values.popitem(last=False)
                self._total_chars -= _candidate_cache_chars(evicted)
                self._eviction_count += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entry_count": len(self._values),
                "total_chars": self._total_chars,
                "max_entries": self._max_entries,
                "max_chars": self._max_chars,
                "eviction_count": self._eviction_count,
                "skipped_oversize_entries": self._skipped_oversize_entries,
            }

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._total_chars = 0


DEFAULT_SYSTEM_PROMPT = (
    "You are Pulsara, an agentic coding runtime. Work carefully inside the current "
    "workspace, use tools when needed, and provide concise final answers."
)


def render_plan_revision_instruction(user_feedback: str) -> str:
    feedback = user_feedback.strip() or "(no additional feedback text was provided)"
    return (
        "Plan revision is still pending. The user requested a revision with this feedback:\n"
        f"{feedback}\n\n"
        "You must now present the revised plan by calling exit_plan. Do not provide a plain-text "
        "final answer or implementation summary. Only call ask_plan_question if a new material "
        "ambiguity genuinely blocks the revised plan."
    )


def build_context_candidate_source_selections(
    *,
    subagent_graph: "SubagentGraphState",
    semantic_source: SubagentGraphSemanticSourceFact,
    policy: ContextCandidateCollectionPolicyFact,
) -> tuple[ContextCandidateSourceSelectionFact, ...]:
    from pulsara_agent.runtime.long_horizon.reducer_contract import (
        graph_state_semantic_fingerprint,
    )
    from pulsara_agent.runtime.subagent.reducer import pending_subagent_result_ids

    if not subagent_graph.consistent:
        raise ValueError(
            "candidate source selection requires a consistent restored graph"
        )
    if (
        semantic_source.graph_state_semantic_fingerprint
        != graph_state_semantic_fingerprint(subagent_graph)
    ):
        raise ValueError("candidate source selection semantic graph mismatch")
    eligible_ids = pending_subagent_result_ids(subagent_graph)
    max_results = policy.max_subagent_results_per_parent_compile
    selected = eligible_ids[:max_results] if max_results > 0 else ()
    omitted = len(eligible_ids) - len(selected)
    if not eligible_ids:
        reason_code = "no_eligible_sources"
    elif omitted == 0:
        reason_code = "selected_all"
    else:
        reason_code = "policy_limit"
    payload = {
        "source_instance_id": "subagent:results",
        "eligible_source_count": len(eligible_ids),
        "selected_source_ids": selected,
        "omitted_source_count": omitted,
        "reason_code": reason_code,
        "policy_fingerprint": policy.policy_fingerprint,
        "subagent_graph_semantic_source": semantic_source,
    }
    return (
        ContextCandidateSourceSelectionFact(
            **payload,
            selection_fingerprint=context_fingerprint(
                "context-candidate-source-selection:v1", payload
            ),
        ),
    )


def collect_context_candidates(
    *,
    snapshot: ContextFactSnapshotFact,
    cache: ContextLifecycleCachePort | None = None,
    hydrated_contents: dict[str, str] | None = None,
) -> PreparedContextCandidateCollection:
    """Apply bounded policy to registry-owned candidate facts."""

    candidates = tuple(
        sorted(
            snapshot.context_source_candidates,
            key=lambda candidate: (
                not candidate.required,
                candidate.priority,
                candidate.source_id.value,
                candidate.source_instance_id,
                candidate.attribution.semantic.candidate_key,
            ),
        )
    )
    semantic_keys = tuple(
        (
            item.source_id.value,
            item.source_instance_id,
            item.attribution.semantic.candidate_key,
            item.attribution.semantic.source_revision.source_revision_id,
        )
        for item in candidates
    )
    if len(semantic_keys) != len(set(semantic_keys)):
        raise ValueError("ContextSource candidate semantic keys must be unique")
    policy = snapshot.compile_policy.candidate_collection
    selections_by_source = {
        item.source_instance_id: item for item in snapshot.candidate_source_selections
    }
    selected_candidates: list[ContextSectionCandidate] = []
    decisions: list[ContextCandidateCollectionDecisionFact] = []
    physical_policy = snapshot.context_source_physical_input_policy
    aggregate_utf8_bytes = 0

    for candidate in candidates:
        validate_candidate_against_snapshot(
            snapshot=snapshot,
            candidate=candidate,
            hydrated_contents=hydrated_contents,
            require_hydrated_content=False,
        )
        candidate_utf8_bytes, has_model_visible_content = _candidate_content_metadata(
            candidate
        )
        if not has_model_visible_content:
            continue
        selection = (
            selections_by_source.get("subagent:results")
            if candidate.source_id.value == "subagent_result"
            else selections_by_source.get(candidate.source_instance_id)
        )
        selected_ids = (
            selection.selected_source_ids
            if selection is not None
            else (candidate.source_instance_id,)
        )
        omitted = selection.omitted_source_count if selection is not None else 0
        reason = selection.reason_code if selection is not None else "selected"
        if (
            candidate_utf8_bytes
            > physical_policy.max_token_budget_admissible_utf8_bytes
        ):
            if candidate.required:
                raise ValueError(
                    "required context candidate exceeds resolved physical input quote"
                )
            decisions.append(
                _collection_decision(
                    source_kind=candidate.source_kind,
                    selected_source_ids=(),
                    omitted_source_count=max(1, len(selected_ids) + omitted),
                    reason_code="source_physical_item_limit",
                    policy_fingerprint=policy.policy_fingerprint,
                )
            )
            continue
        prospective_count = len(selected_candidates) + 1
        prospective_utf8_bytes = aggregate_utf8_bytes + candidate_utf8_bytes
        prospective_canonical_bytes = (
            prospective_utf8_bytes
            * physical_policy.canonical_encoding_expansion_numerator
            + physical_policy.canonical_encoding_expansion_denominator
            - 1
        ) // physical_policy.canonical_encoding_expansion_denominator
        prospective_canonical_bytes += (
            prospective_count * physical_policy.structural_overhead_bytes_per_unit
        )
        physical_limit_reached = (
            prospective_count > physical_policy.max_source_entries
            or prospective_canonical_bytes
            > physical_policy.max_canonical_materialization_bytes
            or prospective_canonical_bytes
            > physical_policy.max_hydrated_working_set_bytes
        )
        if physical_limit_reached:
            if candidate.required:
                raise ValueError(
                    "required context candidates exceed resolved physical working set"
                )
            decisions.append(
                _collection_decision(
                    source_kind=candidate.source_kind,
                    selected_source_ids=(),
                    omitted_source_count=max(1, len(selected_ids) + omitted),
                    reason_code="source_physical_working_set_limit",
                    policy_fingerprint=policy.policy_fingerprint,
                )
            )
            continue
        selected_candidates.append(candidate)
        decisions.append(
            _collection_decision(
                source_kind=candidate.source_kind,
                selected_source_ids=selected_ids,
                omitted_source_count=omitted,
                reason_code=reason,
                policy_fingerprint=policy.policy_fingerprint,
            )
        )
        aggregate_utf8_bytes = prospective_utf8_bytes

    selected_subagent_results = {
        item.attribution.semantic.candidate_key
        for item in selected_candidates
        if item.source_id.value == "subagent_result"
    }
    for selection in snapshot.candidate_source_selections:
        if selection.source_instance_id == "subagent:results" and (
            selected_subagent_results
        ):
            continue
        decisions.append(
            _collection_decision(
                source_kind=_selection_source_kind(selection.source_instance_id),
                selected_source_ids=selection.selected_source_ids,
                omitted_source_count=selection.omitted_source_count,
                reason_code=selection.reason_code,
                policy_fingerprint=selection.policy_fingerprint,
            )
        )

    prepared, writes, operational = prepare_context_candidates(
        snapshot=snapshot,
        candidates=tuple(selected_candidates),
        collection_decisions=tuple(decisions),
        cache=cache,
        hydrated_contents=hydrated_contents,
        require_hydrated_content=False,
    )
    return PreparedContextCandidateCollection(
        prepared=prepared,
        cache_writes=writes,
        operational_diagnostics=operational,
    )


def validate_candidate_against_snapshot(
    *,
    snapshot: ContextFactSnapshotFact,
    candidate: ContextSectionCandidate,
    hydrated_contents: dict[str, str] | None = None,
    require_hydrated_content: bool = True,
) -> None:
    matches = tuple(
        item
        for item in snapshot.context_source_candidates
        if item.candidate_fingerprint == candidate.candidate_fingerprint
    )
    if matches != (candidate,):
        raise ValueError("candidate is not the exact snapshot-owned source fact")
    attribution = candidate.attribution
    semantic = attribution.semantic
    if semantic.source_id.value != attribution.source_contract_id:
        raise ValueError("candidate source contract ID differs from semantic owner")
    if not attribution.source_contract_version:
        raise ValueError("candidate source contract version is empty")
    if not attribution.authority_horizons:
        raise ValueError("candidate lacks per-ledger authority horizons")
    horizon_by_owner = {
        item.runtime_session_id: item for item in attribution.authority_horizons
    }
    for ref in attribution.source_event_refs:
        horizon = horizon_by_owner.get(ref.runtime_session_id)
        if horizon is None or ref.sequence > horizon.through_sequence:
            raise ValueError("candidate ref exceeds source authority horizon")
    content = context_source_payload_content(semantic.payload)
    if require_hydrated_content or not isinstance(
        content, ArtifactContextSourceContentSemanticFact
    ):
        render_context_source_candidate(
            candidate,
            hydrated_contents=hydrated_contents,
        )


def prepare_context_candidates(
    *,
    snapshot: ContextFactSnapshotFact,
    candidates: tuple[ContextSectionCandidate, ...],
    collection_decisions: tuple[ContextCandidateCollectionDecisionFact, ...],
    cache: ContextLifecycleCachePort | None,
    hydrated_contents: dict[str, str] | None = None,
    require_hydrated_content: bool = False,
) -> tuple[
    PreparedContextCandidateSet,
    tuple[ContextLifecycleCacheWriteCandidate, ...],
    tuple[ContextLifecycleCacheOperationalDiagnostic, ...],
]:
    """Resolve lifecycle facts without mutating the cache or compile input."""

    policy = snapshot.compile_policy.candidate_collection
    entries: list[PreparedContextCandidateEntryFact] = []
    invalidations: list[ContextCandidateInvalidationFact] = []
    writes: list[ContextLifecycleCacheWriteCandidate] = []
    operational: list[ContextLifecycleCacheOperationalDiagnostic] = []
    for candidate in candidates:
        validate_candidate_against_snapshot(
            snapshot=snapshot,
            candidate=candidate,
            hydrated_contents=hydrated_contents,
            require_hydrated_content=require_hydrated_content,
        )
        key = _lifecycle_key(snapshot=snapshot, candidate=candidate)
        try:
            cached = cache.get(key) if cache is not None and key is not None else None
        except Exception as exc:
            cached = None
            operational.append(
                ContextLifecycleCacheOperationalDiagnostic(
                    source_instance_id=candidate.source_instance_id,
                    operation="read",
                    error=exc,
                )
            )
        replacement: str | None = None
        if key is None:
            status = "not_cacheable"
            reason = "audit_only_candidate"
        elif (
            cached is not None
            and cached.attribution.semantic == candidate.attribution.semantic
        ):
            status = "reused"
            reason = "cache_identity_confirmed"
        else:
            status = "freshly_collected"
            reason = "cache_miss" if cached is None else "dependency_payload_changed"
            writes.append(
                ContextLifecycleCacheWriteCandidate(key=key, candidate=candidate)
            )
            if cached is not None:
                replacement = cached.candidate_fingerprint
                invalidation_payload = {
                    "candidate_id": candidate.candidate_id,
                    "old_candidate_fingerprint": cached.candidate_fingerprint,
                    "new_candidate_fingerprint": candidate.candidate_fingerprint,
                    "reason_code": "dependency_payload_changed",
                }
                invalidations.append(
                    ContextCandidateInvalidationFact(
                        **invalidation_payload,
                        invalidation_fingerprint=context_fingerprint(
                            "context-candidate-invalidation:v1",
                            invalidation_payload,
                        ),
                    )
                )
        lifecycle_payload = {
            "candidate_id": candidate.candidate_id,
            "status": status,
            "reason_code": reason,
            "cache_key": key,
            "replaced_candidate_fingerprint": replacement,
        }
        lifecycle = ContextCandidateLifecycleDecisionFact(
            **lifecycle_payload,
            decision_fingerprint=context_fingerprint(
                "context-candidate-lifecycle-decision:v1", lifecycle_payload
            ),
        )
        entries.append(
            PreparedContextCandidateEntryFact(
                candidate=candidate,
                lifecycle=lifecycle,
            )
        )
    set_payload = {
        "policy": policy,
        "entries": tuple(entries),
        "collection_decisions": collection_decisions,
        "invalidations": tuple(invalidations),
    }
    return (
        PreparedContextCandidateSet(
            **set_payload,
            candidate_set_fingerprint=context_fingerprint(
                "prepared-context-candidate-set:v1", set_payload
            ),
        ),
        tuple(writes),
        tuple(operational),
    )


def _lifecycle_key(
    *,
    snapshot: ContextFactSnapshotFact,
    candidate: ContextSectionCandidate,
) -> ContextCandidateLifecycleKeyFact | None:
    semantic = candidate.attribution.semantic
    lifecycle_kind = semantic.lifecycle.lifecycle_kind
    if lifecycle_kind == "audit_only":
        return None
    if lifecycle_kind == "generation_root":
        stability = "stable"
        scope_id = snapshot.identity.runtime_session_id
    elif lifecycle_kind == "append_once":
        stability = "run"
        scope_id = snapshot.identity.run_id
    else:
        stability = "step"
        scope_id = snapshot.identity.run_id
    dependency = context_fingerprint(
        "context-source-lifecycle-cache-dependency:v1",
        (
            semantic.source_revision.revision_fingerprint,
            candidate.attribution.source_contract_fingerprint,
            semantic.semantic_fingerprint,
        ),
    )
    return ContextCandidateLifecycleKeyFact(
        source_instance_id=candidate.source_instance_id,
        candidate_id=candidate.candidate_id,
        stability=stability,
        scope_id=scope_id,
        dependency_fingerprint=dependency,
        policy_version=snapshot.compile_policy.allocation.lifecycle_policy_version,
    )


def _collection_decision(
    *,
    source_kind: str,
    selected_source_ids: tuple[str, ...],
    omitted_source_count: int,
    reason_code: str,
    policy_fingerprint: str,
) -> ContextCandidateCollectionDecisionFact:
    payload = {
        "source_kind": source_kind,
        "selected_source_ids": selected_source_ids,
        "omitted_source_count": omitted_source_count,
        "reason_code": reason_code,
        "policy_fingerprint": policy_fingerprint,
    }
    return ContextCandidateCollectionDecisionFact(
        **payload,
        decision_fingerprint=context_fingerprint(
            "context-candidate-collection-decision:v1", payload
        ),
    )


def _selection_source_kind(source_instance_id: str) -> str:
    if source_instance_id == "subagent:results":
        return "subagent_results"
    raise ValueError(f"unsupported candidate source selection: {source_instance_id}")


def _candidate_cache_chars(candidate: ContextSectionCandidate) -> int:
    content = context_source_payload_content(candidate.attribution.semantic.payload)
    if isinstance(content, ArtifactContextSourceContentSemanticFact):
        return content.expected_chars
    return len(render_context_source_candidate(candidate))


def _candidate_content_metadata(
    candidate: ContextSectionCandidate,
) -> tuple[int, bool]:
    content = context_source_payload_content(candidate.attribution.semantic.payload)
    if isinstance(content, ArtifactContextSourceContentSemanticFact):
        return content.expected_utf8_bytes, content.expected_chars > 0
    rendered = render_context_source_candidate(candidate)
    return len(rendered.encode("utf-8")), bool(rendered)


__all__ = [
    "ContextLifecycleCachePort",
    "ContextLifecycleCacheOperationalDiagnostic",
    "ContextLifecycleCacheWriteCandidate",
    "DEFAULT_SYSTEM_PROMPT",
    "InMemoryContextLifecycleCache",
    "PreparedContextCandidateCollection",
    "build_context_candidate_source_selections",
    "collect_context_candidates",
    "prepare_context_candidates",
    "render_plan_revision_instruction",
    "validate_candidate_against_snapshot",
]
