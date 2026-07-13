"""Typed candidate collection before full ContextSource ownership."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from typing import Protocol

from pulsara_agent.event import (
    EventType,
    PlanExitResolvedEvent,
    ProjectionReadyEvent,
    SubagentRunCompletedEvent,
)
from pulsara_agent.primitives.context import (
    ContextCandidateCollectionDecisionFact,
    ContextCandidateAuthorityFact,
    ContextCandidateCollectionPolicyFact,
    ContextCandidateSourceSelectionFact,
    ContextCandidateInvalidationFact,
    ContextCandidateLifecycleKeyFact,
    ContextChannelFact,
    ContextFactSnapshotFact,
    ContextInlineTextFact,
    ContextCompileTimingFact,
    ContextRuntimeEnvironmentFact,
    ContextSectionCandidate,
    ContextSourceTimingFact,
    PreparedContextCandidateEntryFact,
    PreparedContextCandidateSet,
    ContextCandidateLifecycleDecisionFact,
    context_fingerprint,
)
from pulsara_agent.primitives.capability import CapabilityExposureSnapshotFact
from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.runtime.context_input.event_slice import ContextEventSlice


@dataclass(frozen=True, slots=True)
class _ContextCandidateSourceText:
    component_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ContextCandidateCollectionInput:
    system_prompt: str
    memory_hook_prompt: str | None = None
    capability_catalog: str | None = None
    capability_active_skill: str | None = None
    plan_workflow: str | None = None

    def __post_init__(self) -> None:
        if not self.system_prompt:
            raise ValueError("candidate collection input requires system prompt")

    def candidate_texts(self) -> tuple[_ContextCandidateSourceText, ...]:
        values = (
            _ContextCandidateSourceText("system:prompt", self.system_prompt),
            _ContextCandidateSourceText(
                "memory:hook_prompt", self.memory_hook_prompt or ""
            ),
            _ContextCandidateSourceText(
                "capability:catalog", self.capability_catalog or ""
            ),
            _ContextCandidateSourceText(
                "capability:active_skill", self.capability_active_skill or ""
            ),
            _ContextCandidateSourceText("plan:workflow", self.plan_workflow or ""),
        )
        return tuple(item for item in values if item.text)


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


def build_context_candidate_authorities(
    *,
    sources: ContextCandidateCollectionInput,
    static_instructions: tuple,
    projections: tuple,
    capability_snapshot: CapabilityExposureSnapshotFact,
    plan_snapshot,
    event_slice: ContextEventSlice,
    run_id: str,
    runtime_environment: ContextRuntimeEnvironmentFact,
    compile_timing: ContextCompileTimingFact,
    source_selections: tuple[ContextCandidateSourceSelectionFact, ...],
) -> tuple[ContextCandidateAuthorityFact, ...]:
    """Freeze the only candidate bytes/attribution accepted by the compiler."""

    static_by_source = {item.source_id: item for item in static_instructions}
    projections_by_kind = {item.projection_kind: item for item in projections}
    selections_by_source = {
        item.source_instance_id: item for item in source_selections
    }
    source_items = list(sources.candidate_texts())
    source_items.append(_ContextCandidateSourceText("runtime_context", ""))
    plan_revision_ref = _latest_pending_plan_revision_ref(
        event_slice=event_slice,
        plan_snapshot=plan_snapshot,
        run_id=run_id,
    )
    if plan_revision_ref is not None:
        source_items.append(_ContextCandidateSourceText("plan:revision", ""))
    if "memory" in projections_by_kind:
        source_items.append(_ContextCandidateSourceText("memory:projection", ""))
    if "subagent_results" in projections_by_kind:
        source_items.append(
            _ContextCandidateSourceText(
                "subagent:results",
                "",
            )
        )
    authorities: list[ContextCandidateAuthorityFact] = []
    for source in source_items:
        spec = _source_spec(source.component_id)
        source_kind = spec[0]
        source_refs, artifact_ids, dependency = _source_attribution_parts(
            component_id=source.component_id,
            projection_kind=spec[6],
            projections=projections_by_kind,
            static_by_source=static_by_source,
            plan_snapshot=plan_snapshot,
            plan_revision_ref=plan_revision_ref,
            runtime_environment=runtime_environment,
        )
        text = _canonical_authority_text(
            source=source,
            source_kind=source_kind,
            source_refs=source_refs,
            event_slice=event_slice,
            runtime_environment=runtime_environment,
            compile_timing=compile_timing,
            source_selection=selections_by_source.get(source.component_id),
        )
        if source_kind in {"capability_catalog", "capability_active_skill"}:
            projection = (
                capability_snapshot.semantic.catalog_projection
                if source_kind == "capability_catalog"
                else capability_snapshot.semantic.active_skill_projection
            )
            actual_raw_fingerprint = (
                f"sha256:{sha256(text.encode('utf-8')).hexdigest()}"
            )
            if (
                projection.rendered_prompt_fingerprint != actual_raw_fingerprint
                or projection.rendered_prompt_chars != len(text)
                or tuple(
                    item
                    for item in (projection.rendered_prompt_artifact_id,)
                    if item is not None
                )
                != artifact_ids
            ):
                raise ValueError("capability candidate differs from frozen projection")
        if source.component_id == "system:prompt":
            static = static_by_source.get("base_system_instruction")
            if static is None or static.content_fingerprint != sha256_fingerprint(
                "context-static-instruction-content:v1", text
            ):
                raise ValueError("system candidate differs from frozen instruction")
        if source.component_id == "memory:hook_prompt":
            static = static_by_source.get("memory_scope_instruction")
            if static is None or static.content_fingerprint != sha256_fingerprint(
                "context-static-instruction-content:v1", text
            ):
                raise ValueError(
                    "memory hook candidate differs from frozen instruction"
                )
        stability = spec[4] if dependency is not None else "ephemeral"
        timing = _source_timing_from_authority(
            event_slice=event_slice,
            runtime_environment=runtime_environment,
            source_refs=source_refs,
            component_id=source.component_id,
        )
        payload = {
            "source_instance_id": source.component_id,
            "source_kind": source_kind,
            "source_fact_refs": source_refs,
            "source_artifact_ids": artifact_ids,
            "channel": spec[1],
            "priority": spec[2],
            "required": spec[3],
            "stability": stability,
            "lowering_kind": spec[5],
            "lifecycle_dependency_fingerprint": dependency,
            "model_visible_text": text,
            "model_visible_content_fingerprint": context_fingerprint(
                "context-inline-text:v1", text
            ),
            "model_visible_chars": len(text),
            "source_timing": timing,
        }
        authorities.append(
            ContextCandidateAuthorityFact(
                **payload,
                authority_fingerprint=context_fingerprint(
                    "context-candidate-authority:v1", payload
                ),
            )
        )
    return tuple(sorted(authorities, key=lambda item: item.source_instance_id))


def build_context_candidate_source_selections(
    *,
    event_slice: ContextEventSlice,
    policy: ContextCandidateCollectionPolicyFact,
) -> tuple[ContextCandidateSourceSelectionFact, ...]:
    # Local import keeps RuntimeSession -> context_input initialization acyclic.
    from pulsara_agent.runtime.subagent.reducer import (
        fold_subagent_graph,
        pending_subagent_result_ids,
    )

    graph = fold_subagent_graph(
        frozen.decode_owned() for frozen in event_slice.events
    )
    if not graph.consistent or graph.through_sequence != event_slice.through_sequence:
        raise ValueError(
            "candidate source selection requires a consistent canonical graph slice"
        )
    eligible_ids = pending_subagent_result_ids(graph)
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
        "source_from_sequence": event_slice.from_sequence,
        "source_through_sequence": event_slice.through_sequence,
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
) -> PreparedContextCandidateCollection:
    """Freeze producer projections into the typed ingress shape.

    This is deliberately not a source registry.  It gives every current
    non-transcript producer a typed, fingerprinted carrier without prematurely
    moving producer ownership.
    """

    authorities = tuple(
        sorted(
            snapshot.candidate_authorities,
            key=lambda authority: (
                not authority.required,
                authority.priority,
                authority.source_instance_id,
            ),
        )
    )
    source_ids = tuple(item.source_instance_id for item in authorities)
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("candidate authority source IDs must be unique")
    policy = snapshot.compile_policy.candidate_collection
    selections_by_source = {
        item.source_instance_id: item
        for item in snapshot.candidate_source_selections
    }
    candidates: list[ContextSectionCandidate] = []
    decisions: list[ContextCandidateCollectionDecisionFact] = []
    emitted_selection_decisions: set[str] = set()
    aggregate_chars = 0

    for authority in authorities:
        text = authority.model_visible_text
        if not text:
            continue
        selection = selections_by_source.get(authority.source_instance_id)
        selected_source_ids = (
            selection.selected_source_ids
            if selection is not None
            else (authority.source_instance_id,)
        )
        omitted_source_count = (
            selection.omitted_source_count if selection is not None else 0
        )
        collection_reason_code = (
            selection.reason_code if selection is not None else "selected"
        )
        if len(text) > policy.max_inline_candidate_chars:
            if authority.required:
                raise ValueError("required context candidate exceeds inline cap")
            decisions.append(
                _collection_decision(
                    source_kind=authority.source_kind,
                    selected_source_ids=(),
                    omitted_source_count=max(
                        1,
                        len(selected_source_ids) + omitted_source_count,
                    ),
                    reason_code="inline_candidate_char_cap",
                    policy_fingerprint=policy.policy_fingerprint,
                )
            )
            if selection is not None:
                emitted_selection_decisions.add(selection.source_instance_id)
            continue
        if aggregate_chars + len(text) > policy.max_aggregate_candidate_chars:
            if authority.required:
                raise ValueError("required context candidates exceed aggregate cap")
            decisions.append(
                _collection_decision(
                    source_kind=authority.source_kind,
                    selected_source_ids=(),
                    omitted_source_count=max(
                        1,
                        len(selected_source_ids) + omitted_source_count,
                    ),
                    reason_code="aggregate_candidate_char_cap",
                    policy_fingerprint=policy.policy_fingerprint,
                )
            )
            if selection is not None:
                emitted_selection_decisions.add(selection.source_instance_id)
            continue
        source_refs = authority.source_fact_refs
        artifact_ids = authority.source_artifact_ids
        dependency = authority.lifecycle_dependency_fingerprint
        source_kind = authority.source_kind
        inline = ContextInlineTextFact(
            text=text,
            chars=len(text),
            content_fingerprint=context_fingerprint("context-inline-text:v1", text),
        )
        candidate_id = f"context-candidate:{authority.source_instance_id}"
        payload = {
            "schema_version": "context-candidate:v1",
            "candidate_id": candidate_id,
            "source_kind": source_kind,
            "source_instance_id": authority.source_instance_id,
            "source_fact_refs": source_refs,
            "source_artifact_ids": artifact_ids,
            "channel": authority.channel,
            "priority": authority.priority,
            "required": authority.required,
            "stability": authority.stability,
            "lifecycle_dependency_fingerprint": dependency,
            "lowering_kind": authority.lowering_kind,
            "payload": inline,
            "source_timing": authority.source_timing,
        }
        semantic_payload = {
            key: value for key, value in payload.items() if key != "candidate_id"
        }
        semantic = context_fingerprint(
            "context-section-candidate-semantic:v1", semantic_payload
        )
        fact_payload = {**payload, "semantic_fingerprint": semantic}
        candidate = ContextSectionCandidate(
            **fact_payload,
            candidate_fingerprint=context_fingerprint(
                "context-section-candidate-fact:v1", fact_payload
            ),
        )
        candidates.append(candidate)
        decisions.append(
            _collection_decision(
                source_kind=source_kind,
                selected_source_ids=selected_source_ids,
                omitted_source_count=omitted_source_count,
                reason_code=collection_reason_code,
                policy_fingerprint=policy.policy_fingerprint,
            )
        )
        if selection is not None:
            emitted_selection_decisions.add(selection.source_instance_id)
        aggregate_chars += len(text)

    for selection in snapshot.candidate_source_selections:
        if selection.source_instance_id in emitted_selection_decisions:
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
        candidates=tuple(candidates),
        collection_decisions=tuple(decisions),
        cache=cache,
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
) -> None:
    authorities = {
        item.source_instance_id: item for item in snapshot.candidate_authorities
    }
    authority = authorities.get(candidate.source_instance_id)
    if authority is None:
        raise ValueError("candidate lacks snapshot authority")
    comparable = (
        candidate.source_kind,
        candidate.source_fact_refs,
        candidate.source_artifact_ids,
        candidate.channel,
        candidate.priority,
        candidate.required,
        candidate.stability,
        candidate.lowering_kind,
        candidate.lifecycle_dependency_fingerprint,
        candidate.source_timing,
    )
    expected = (
        authority.source_kind,
        authority.source_fact_refs,
        authority.source_artifact_ids,
        authority.channel,
        authority.priority,
        authority.required,
        authority.stability,
        authority.lowering_kind,
        authority.lifecycle_dependency_fingerprint,
        authority.source_timing,
    )
    if comparable != expected:
        raise ValueError("candidate attribution differs from snapshot authority")
    if not isinstance(candidate.payload, ContextInlineTextFact):
        raise ValueError("compiler candidate must be materialized inline text")
    if (
        candidate.payload.text != authority.model_visible_text
        or candidate.payload.content_fingerprint
        != authority.model_visible_content_fingerprint
        or candidate.payload.chars != authority.model_visible_chars
    ):
        raise ValueError("candidate payload differs from snapshot authority")
    _validate_candidate_source_authority(
        snapshot=snapshot,
        authority=authority,
        text=candidate.payload.text,
    )


def _validate_candidate_source_authority(
    *,
    snapshot: ContextFactSnapshotFact,
    authority: ContextCandidateAuthorityFact,
    text: str,
) -> None:
    """Join snapshot-owned candidate bytes to their canonical source fact."""

    if authority.source_kind == "system":
        static = next(
            (
                item
                for item in snapshot.static_instructions
                if item.source_id == "base_system_instruction"
            ),
            None,
        )
        if (
            static is None
            or authority.source_fact_refs
            or authority.source_artifact_ids != (static.content_artifact_id,)
            or authority.lifecycle_dependency_fingerprint != static.fact_fingerprint
            or static.chars != len(text)
            or static.content_fingerprint
            != sha256_fingerprint("context-static-instruction-content:v1", text)
        ):
            raise ValueError(
                "system candidate differs from static instruction authority"
            )
        return

    projection_kind = {
        "memory_projection": "memory",
        "capability_catalog": "capability_catalog",
        "capability_active_skill": "capability_active_skill",
        "recovery": "recovery",
        "subagent_results": "subagent_results",
    }.get(authority.source_kind)
    if projection_kind is not None:
        projection_ref = next(
            (
                item
                for item in snapshot.projections
                if item.projection_kind == projection_kind
            ),
            None,
        )
        if (
            projection_ref is None
            or authority.source_fact_refs != projection_ref.source_event_refs
            or authority.source_artifact_ids != projection_ref.source_artifact_ids
            or authority.lifecycle_dependency_fingerprint
            != projection_ref.semantic_fingerprint
        ):
            raise ValueError("candidate differs from projection authority")
        if authority.source_kind in {
            "capability_catalog",
            "capability_active_skill",
        }:
            projection = (
                snapshot.capability_snapshot.semantic.catalog_projection
                if authority.source_kind == "capability_catalog"
                else snapshot.capability_snapshot.semantic.active_skill_projection
            )
            raw_fingerprint = f"sha256:{sha256(text.encode('utf-8')).hexdigest()}"
            if (
                projection.rendered_prompt_fingerprint != raw_fingerprint
                or projection.rendered_prompt_chars != len(text)
                or tuple(
                    item
                    for item in (projection.rendered_prompt_artifact_id,)
                    if item is not None
                )
                != authority.source_artifact_ids
            ):
                raise ValueError(
                    "capability candidate differs from exposure projection authority"
                )
        return

    if authority.source_kind == "plan":
        if authority.source_instance_id == "plan:revision":
            if len(authority.source_fact_refs) != 1:
                raise ValueError(
                    "plan revision candidate requires one durable decision"
                )
            decision_ref = authority.source_fact_refs[0]
            if decision_ref.event_type != EventType.PLAN_EXIT_RESOLVED.value:
                raise ValueError(
                    "plan revision candidate ref is not PlanExitResolved"
                )
            expected_refs = (decision_ref,)
            expected_dependency = context_fingerprint(
                "plan-revision-candidate-authority:v1",
                {
                    "plan_snapshot_fingerprint": (
                        snapshot.plan_snapshot.fact_fingerprint
                    ),
                    "revision_event": decision_ref,
                },
            )
        else:
            expected_refs = (
                (snapshot.plan_snapshot.entered_event,)
                if snapshot.plan_snapshot.entered_event is not None
                else ()
            )
            expected_dependency = snapshot.plan_snapshot.fact_fingerprint
        if (
            authority.source_fact_refs != expected_refs
            or authority.source_artifact_ids
            or authority.lifecycle_dependency_fingerprint
            != expected_dependency
        ):
            raise ValueError("plan candidate differs from plan snapshot authority")
        return

    if authority.source_instance_id == "memory:hook_prompt":
        static = next(
            (
                item
                for item in snapshot.static_instructions
                if item.source_id == "memory_scope_instruction"
            ),
            None,
        )
        if (
            static is None
            or authority.source_fact_refs
            or authority.source_artifact_ids != (static.content_artifact_id,)
            or authority.lifecycle_dependency_fingerprint != static.fact_fingerprint
            or static.chars != len(text)
            or static.content_fingerprint
            != sha256_fingerprint("context-static-instruction-content:v1", text)
        ):
            raise ValueError(
                "memory hook candidate differs from static instruction authority"
            )
        return

    if authority.source_kind == "runtime_context":
        if authority.source_instance_id != "runtime_context":
            raise ValueError("unknown runtime-context authority")
        if (
            authority.source_fact_refs
            or authority.source_artifact_ids
            or authority.lifecycle_dependency_fingerprint
            != snapshot.runtime_environment.fact_fingerprint
            or text
            != render_runtime_context_from_facts(
                runtime_environment=snapshot.runtime_environment,
                compile_timing=snapshot.timing,
            )
        ):
            raise ValueError(
                "runtime context candidate differs from environment authority"
            )


def prepare_context_candidates(
    *,
    snapshot: ContextFactSnapshotFact,
    candidates: tuple[ContextSectionCandidate, ...],
    collection_decisions: tuple[ContextCandidateCollectionDecisionFact, ...],
    cache: ContextLifecycleCachePort | None,
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
        validate_candidate_against_snapshot(snapshot=snapshot, candidate=candidate)
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
            reason = "ephemeral_candidate"
        elif cached is not None and cached == candidate:
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
    dependency = candidate.lifecycle_dependency_fingerprint
    if candidate.stability == "ephemeral" or dependency is None:
        return None
    if candidate.stability == "stable":
        scope_id = snapshot.identity.runtime_session_id
    elif candidate.stability == "run":
        scope_id = snapshot.identity.run_id
    else:
        scope_id = ":".join(
            (
                snapshot.identity.run_id,
                str(snapshot.identity.model_call_index),
                str(snapshot.identity.compile_attempt_index),
            )
        )
    return ContextCandidateLifecycleKeyFact(
        source_instance_id=candidate.source_instance_id,
        candidate_id=candidate.candidate_id,
        stability=candidate.stability,
        scope_id=scope_id,
        dependency_fingerprint=dependency,
        policy_version=snapshot.compile_policy.allocation.lifecycle_policy_version,
    )


def _source_spec(component_id: str):
    if component_id == "system:prompt":
        return (
            "system",
            ContextChannelFact.SYSTEM,
            0,
            True,
            "stable",
            "system_instruction",
            None,
        )
    if component_id.startswith("capability:catalog"):
        return (
            "capability_catalog",
            ContextChannelFact.LEADING_USER,
            30,
            False,
            "run",
            "leading_user_context",
            "capability_catalog",
        )
    if component_id.startswith("capability:active_skill"):
        return (
            "capability_active_skill",
            ContextChannelFact.SYSTEM,
            31,
            False,
            "run",
            "system_instruction",
            "capability_active_skill",
        )
    if component_id.startswith("memory:projection"):
        return (
            "memory_projection",
            ContextChannelFact.LEADING_USER,
            40,
            False,
            "step",
            "leading_user_context",
            "memory",
        )
    if component_id.startswith("subagent:results"):
        return (
            "subagent_results",
            ContextChannelFact.LEADING_USER,
            60,
            False,
            "step",
            "leading_user_context",
            "subagent_results",
        )
    if component_id.startswith("plan:"):
        return (
            "plan",
            ContextChannelFact.LEADING_USER,
            10,
            True,
            "run",
            "leading_user_context",
            None,
        )
    return (
        "runtime_context",
        ContextChannelFact.LEADING_USER,
        20,
        False,
        "ephemeral",
        "leading_user_context",
        None,
    )


def _source_attribution_parts(
    *,
    component_id: str,
    projection_kind: str | None,
    projections,
    static_by_source,
    plan_snapshot,
    plan_revision_ref,
    runtime_environment,
):
    if component_id == "system:prompt":
        static = static_by_source.get("base_system_instruction")
        if static is None:
            raise ValueError("system candidate requires frozen static instruction")
        return (), (static.content_artifact_id,), static.fact_fingerprint
    if component_id == "memory:hook_prompt":
        static = static_by_source.get("memory_scope_instruction")
        if static is None:
            raise ValueError(
                "memory hook candidate requires frozen static instruction"
            )
        return (), (static.content_artifact_id,), static.fact_fingerprint
    if component_id == "runtime_context":
        return (), (), runtime_environment.fact_fingerprint
    if projection_kind is not None and projection_kind in projections:
        projection = projections[projection_kind]
        return (
            projection.source_event_refs,
            projection.source_artifact_ids,
            projection.semantic_fingerprint,
        )
    if component_id.startswith("plan:"):
        if component_id == "plan:revision":
            if plan_revision_ref is None:
                raise ValueError(
                    "plan revision candidate requires durable revise decision"
                )
            return (
                (plan_revision_ref,),
                (),
                context_fingerprint(
                    "plan-revision-candidate-authority:v1",
                    {
                        "plan_snapshot_fingerprint": plan_snapshot.fact_fingerprint,
                        "revision_event": plan_revision_ref,
                    },
                ),
            )
        refs = (
            (plan_snapshot.entered_event,)
            if plan_snapshot.entered_event is not None
            else ()
        )
        return refs, (), plan_snapshot.fact_fingerprint
    return (), (), None


def _canonical_authority_text(
    *,
    source: _ContextCandidateSourceText,
    source_kind: str,
    source_refs,
    event_slice: ContextEventSlice,
    runtime_environment: ContextRuntimeEnvironmentFact,
    compile_timing: ContextCompileTimingFact,
    source_selection: ContextCandidateSourceSelectionFact | None,
) -> str:
    if source_kind == "memory_projection":
        if len(source_refs) != 1:
            raise ValueError("memory candidate requires one canonical projection event")
        event = event_slice.event_by_id(source_refs[0].event_id).decode_owned()
        if not isinstance(event, ProjectionReadyEvent):
            raise ValueError("memory candidate authority is not ProjectionReadyEvent")
        return _memory_projection_text_from_event(event)
    if source_kind == "subagent_results":
        events = tuple(
            event_slice.event_by_id(ref.event_id).decode_owned() for ref in source_refs
        )
        if not events or not all(
            isinstance(event, SubagentRunCompletedEvent) for event in events
        ):
            raise ValueError("subagent candidate authority requires completion events")
        result_ids = tuple(event.result_id for event in events)
        if (
            source_selection is None
            or source_selection.source_instance_id != "subagent:results"
            or result_ids != source_selection.selected_source_ids
        ):
            raise ValueError("subagent candidate selection differs from graph facts")
        return _subagent_results_text_from_events(events)
    if source.component_id == "runtime_context":
        return render_runtime_context_from_facts(
            runtime_environment=runtime_environment,
            compile_timing=compile_timing,
        )
    if source.component_id == "plan:revision":
        if len(source_refs) != 1:
            raise ValueError("plan revision candidate requires one decision event")
        event = event_slice.event_by_id(source_refs[0].event_id).decode_owned()
        if not isinstance(event, PlanExitResolvedEvent) or event.decision != "revise":
            raise ValueError("plan revision authority is not a revise decision")
        return render_plan_revision_instruction(event.user_feedback)
    return source.text


def render_runtime_context_from_facts(
    *,
    runtime_environment: ContextRuntimeEnvironmentFact,
    compile_timing: ContextCompileTimingFact,
) -> str:
    """Render runtime context only from the frozen environment/clock facts."""

    local_date = (
        compile_timing.compiled_local_date
        or compile_timing.compiled_at_utc[:10]
    )
    timezone = (
        compile_timing.session_timezone
        or runtime_environment.session_timezone
        or "UTC"
    )
    workspace_kind = runtime_environment.workspace_kind
    workspace_mode = (
        "project workspace; treat workspace facts as durable project context."
        if workspace_kind == "project"
        else "transient scratch workspace; do not treat workspace facts as durable project context."
    )
    return "\n".join(
        [
            "<runtime-context>",
            f"Current date: {local_date}",
            f"Local timezone: {timezone}",
            f"Workspace kind: {workspace_kind} ({workspace_mode})",
            f"Workspace root: {runtime_environment.model_visible_workspace_root}",
            f"Terminal current cwd: {runtime_environment.terminal_current_cwd}",
            "Terminal workdir, when provided, must stay inside workspace_root; when unsure, omit workdir or run pwd.",
            "Relative terminal workdir values resolve from workspace_root.",
            "Read-only filesystem tools may read ordinary text files outside workspace_root, but write/edit tools and terminal workdir remain workspace-scoped.",
            "</runtime-context>",
        ]
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


def _latest_pending_plan_revision_ref(
    *,
    event_slice: ContextEventSlice,
    plan_snapshot,
    run_id: str,
):
    if not plan_snapshot.active:
        return None
    resolutions = [
        (frozen, event)
        for frozen in event_slice.events
        if frozen.event_type == EventType.PLAN_EXIT_RESOLVED
        if isinstance((event := frozen.decode_owned()), PlanExitResolvedEvent)
        and event.run_id == run_id
    ]
    if not resolutions or resolutions[-1][1].decision != "revise":
        return None
    return resolutions[-1][0].to_reference(event_slice.runtime_session_id)


def _memory_projection_text_from_event(event: ProjectionReadyEvent) -> str:
    if event.projection_kind in {"working_context", "mixed"}:
        heading = (
            "Recalled Memory and Recent Working Context "
            "(source=fenced_memory_context; do not write it back as new memory):\n"
            "Recent Working Context is independent from canonical memory search. "
            "An empty memory_search result does not invalidate recent activity "
            "shown here."
        )
    else:
        heading = (
            "Recalled Memory (source=fenced_recalled_memory; do not write it back "
            "as new memory):"
        )
    return "\n\n".join((heading, event.summary))


def _subagent_results_text_from_events(events) -> str:
    lines = [
        "Completed child agent results that have not been explicitly collected with wait_agent:",
    ]
    for event in events:
        lines.extend(
            [
                f"- subagent_run_id: {event.subagent_run_id}",
                f"  result_id: {event.result_id}",
                "  status: completed",
                f"  summary: {event.summary}",
                f"  result_artifact_id: {event.result_artifact_id or 'none'}",
            ]
        )
    return "\n".join(lines)


def _source_timing_from_authority(
    *,
    event_slice: ContextEventSlice,
    runtime_environment: ContextRuntimeEnvironmentFact,
    source_refs,
    component_id: str,
) -> ContextSourceTimingFact:
    if component_id == "memory:projection":
        freshness = "memory_projection"
    elif component_id.startswith("subagent:results"):
        freshness = "subagent_result"
    elif component_id.startswith("capability:") or component_id.startswith(
        "memory:hook_prompt"
    ):
        freshness = "cached_snapshot"
    elif component_id.startswith("runtime_context") or component_id.startswith("plan:"):
        freshness = "current_turn"
    else:
        freshness = "unknown"
    if source_refs:
        frozen = tuple(event_slice.event_by_id(ref.event_id) for ref in source_refs)
        if any(
            ref.runtime_session_id != event_slice.runtime_session_id
            or ref.sequence != event.sequence
            or ref.payload_fingerprint != event.payload_fingerprint
            for ref, event in zip(source_refs, frozen, strict=True)
        ):
            raise ValueError("candidate timing refs differ from authority event slice")
        observed_at = frozen[-1].created_at_utc
        started_at = frozen[0].created_at_utc
        ended_at = frozen[-1].created_at_utc
        clock_source = "event_created_at"
    elif freshness == "current_turn":
        observed_at = runtime_environment.observed_at_utc
        started_at = None
        ended_at = None
        clock_source = "host_clock"
    else:
        observed_at = None
        started_at = None
        ended_at = None
        clock_source = "mixed"
    payload = {
        "observed_at_utc": observed_at,
        "source_started_at_utc": started_at,
        "source_ended_at_utc": ended_at,
        "source_sequence_start": source_refs[0].sequence if source_refs else None,
        "source_sequence_end": source_refs[-1].sequence if source_refs else None,
        "freshness": freshness,
        "clock_source": clock_source,
    }
    return ContextSourceTimingFact(
        **payload,
        timing_fingerprint=context_fingerprint("context-source-timing:v1", payload),
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
    payload = candidate.payload
    if isinstance(payload, ContextInlineTextFact):
        return payload.chars
    return payload.expected_chars


__all__ = [
    "ContextLifecycleCachePort",
    "ContextLifecycleCacheOperationalDiagnostic",
    "ContextLifecycleCacheWriteCandidate",
    "InMemoryContextLifecycleCache",
    "PreparedContextCandidateCollection",
    "DEFAULT_SYSTEM_PROMPT",
    "ContextCandidateCollectionInput",
    "build_context_candidate_authorities",
    "build_context_candidate_source_selections",
    "collect_context_candidates",
    "prepare_context_candidates",
    "render_plan_revision_instruction",
    "render_runtime_context_from_facts",
    "validate_candidate_against_snapshot",
]
