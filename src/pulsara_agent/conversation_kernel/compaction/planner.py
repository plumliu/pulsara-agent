"""Pure one-cut planning for Round 5B context compaction."""

from __future__ import annotations

from dataclasses import replace
from time import monotonic

from pulsara_agent.conversation_kernel.compaction.contracts import (
    ColdRebuildCompactionProjection,
    CompactionPhysicalWorkingSetReport,
    CompactionSourceCompatibility,
    CompleteToolGroup,
    FrozenCompactionCanonicalRead,
    FrozenCompactionSourceView,
    ProtectedTailSelectionFact,
    ProviderPrefixCutProof,
    RecentHumanMessageProof,
    ResolvedCompactionPolicy,
    _compaction_projection_identity_digest,
    _compaction_working_set_identity_digest,
    compaction_summary_message_prefix_fingerprint,
    resolved_compaction_headroom_bounds,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    freeze_compaction_summary_output,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.model_input.contracts import (
    ApprovedPlanMaterializationFact,
    CanonicalInputOriginKind,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    ContextBindingBaseKind,
    FrozenCanonicalCompileSnapshot,
    FrozenContextBindingCompileFact,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    FrozenToolSpec,
    ModelInputCompileBinding,
    PlanApprovedMaterializationDisposition,
    approved_plan_materialization_fingerprint,
    canonical_compile_snapshot_fingerprint,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    context_binding_compile_fact_fingerprint,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendCompileResult,
    FrozenProviderInputEpochView,
    PROVIDER_MESSAGE_LOWERING_CONTRACT,
    provider_input_logical_utf8_bytes,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    freeze_provider_replay_manifest_cut,
)
from pulsara_agent.primitives.context import context_fingerprint


class CompactionPlanningError(RuntimeError):
    """A body-free deterministic compaction planning failure."""


def build_synthetic_compaction_dispatch_read(
    *,
    canonical_read: FrozenCompactionCanonicalRead,
    source_through_sequence: int,
    snapshot_id: str,
    binding_revision_id: str,
    binding_revision_ordinal: int,
    snapshot_body: bytes,
    snapshot_content_digest: str,
    snapshot_content_size: int,
    snapshot_content_media_type: str,
    snapshot_content_codec: str,
    snapshot_blob_id: str | None,
) -> FrozenCanonicalProviderDispatchRead:
    """Build the exact reader-equivalent post-adoption cut without I/O.

    This is the dry-assembly input.  It deliberately mirrors the canonical
    reader's snapshot-base framing, then carries only the exact post-boundary
    canonical suffix and replay manifests.  The real reader result after FULL
    must compare equal to this value before continuity installation.
    """

    if (
        not snapshot_id
        or not binding_revision_id
        or binding_revision_ordinal < 1
        or snapshot_content_size != len(snapshot_body)
        or snapshot_content_codec != "utf-8"
        or not snapshot_content_digest.startswith("sha256:")
    ):
        raise ValueError("synthetic compaction snapshot identity is invalid")
    try:
        snapshot_text = snapshot_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("synthetic compaction snapshot is not UTF-8") from exc
    old_dispatch = canonical_read.dispatch_read
    old_facts = old_dispatch.compile_snapshot
    old_input = old_facts.canonical_input
    if not (
        canonical_read.lineage_base.effective_materialization_lineage_floor
        <= source_through_sequence
        <= old_input.identity.provider_input_through_sequence
    ):
        raise ValueError("synthetic compaction boundary is outside the source cut")
    suffix = tuple(
        item
        for item in old_input.items
        if item.source_entry_sequence is not None
        and source_through_sequence < item.source_entry_sequence
    )
    snapshot_item = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.CONTEXT_SNAPSHOT,
        source_entry_id=None,
        source_entry_sequence=source_through_sequence,
        source_turn_id=None,
        text=snapshot_text,
    )
    items = (snapshot_item, *suffix)
    request_ids = frozenset(
        item.source_entry_id
        for item in suffix
        if item.item_kind is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST
        and item.source_entry_id is not None
    )
    closures = tuple(
        item
        for item in old_input.closures
        if item.assistant_entry_id in request_ids
    )
    late_outcomes = tuple(
        item
        for item in old_input.late_outcomes
        if item.result_entry_sequence > source_through_sequence
    )
    old_identity = old_input.identity
    identity_values = {
        "session_id": old_identity.session_id,
        "turn_id": old_identity.turn_id,
        "initial_entry_id": old_identity.initial_entry_id,
        "context_binding_revision_id": binding_revision_id,
        "provider_input_through_sequence": (
            old_identity.provider_input_through_sequence
        ),
        "conversation_scope_kind": old_identity.conversation_scope_kind,
        "scope_subagent_task_id": old_identity.scope_subagent_task_id,
    }
    identity = CanonicalModelInputIdentity(
        **identity_values,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            **identity_values
        ),
    )
    # CanonicalModelInputSnapshot.canonical_utf8_bytes is the bounded reader
    # hydration quote: decoded entry/result text bytes, not the larger stable
    # semantic-leaf encoding used by compaction range fingerprints.  Mirror
    # that reader contract exactly so dry and post-FULL cuts can compare equal.
    canonical_bytes = len(snapshot_body) + sum(
        len(item.text.encode("utf-8")) for item in suffix
    )
    canonical_input = CanonicalModelInputSnapshot(
        identity=identity,
        items=items,
        canonical_utf8_bytes=canonical_bytes,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=items,
            canonical_utf8_bytes=canonical_bytes,
            closures=closures,
            late_outcomes=late_outcomes,
        ),
        closures=closures,
        late_outcomes=late_outcomes,
    )
    context_base_identity = context_fingerprint(
        "pulsara:context-snapshot-base-semantic-identity:v1",
        {
            "snapshot_id": snapshot_id,
            # Keep the exact canonical-reader representation.  Inline
            # snapshots are represented by the string form of SQL NULL.
            "blob_id": str(snapshot_blob_id),
            "digest": snapshot_content_digest,
            "size": snapshot_content_size,
            "media_type": snapshot_content_media_type,
            "codec": snapshot_content_codec,
            "source_through_sequence": source_through_sequence,
            "lowering": PROVIDER_MESSAGE_LOWERING_CONTRACT,
        },
    )
    binding_values = {
        "binding_revision_id": binding_revision_id,
        "revision_ordinal": binding_revision_ordinal,
        "base_kind": ContextBindingBaseKind.SNAPSHOT,
        "context_snapshot_id": snapshot_id,
        "source_through_sequence": source_through_sequence,
        "context_base_semantic_identity": context_base_identity,
    }
    provisional_binding = FrozenContextBindingCompileFact.__new__(
        FrozenContextBindingCompileFact
    )
    for name, value in binding_values.items():
        object.__setattr__(provisional_binding, name, value)
    object.__setattr__(provisional_binding, "fact_fingerprint", "")
    binding = FrozenContextBindingCompileFact(
        **binding_values,
        fact_fingerprint=context_binding_compile_fact_fingerprint(
            provisional_binding
        ),
    )
    approved = _synthetic_approved_plan_fact(
        old_facts.approved_plan_materialization_fact,
        items=items,
    )
    fact_values = {
        "canonical_input": canonical_input,
        "context_binding_fact": binding,
        "run_permission_snapshot": old_facts.run_permission_snapshot,
        "plan_workflow_fact": old_facts.plan_workflow_fact,
        "plan_handoff_fact": old_facts.plan_handoff_fact,
        "approved_plan_materialization_fact": approved,
        "previous_turn_outcome_fact": old_facts.previous_turn_outcome_fact,
        "tool_observation_freshness_fact": (
            old_facts.tool_observation_freshness_fact
        ),
    }
    provisional_facts = FrozenCanonicalCompileSnapshot.__new__(
        FrozenCanonicalCompileSnapshot
    )
    for name, value in fact_values.items():
        object.__setattr__(provisional_facts, name, value)
    object.__setattr__(provisional_facts, "canonical_read_cut_fingerprint", "")
    facts = FrozenCanonicalCompileSnapshot(
        **fact_values,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional_facts
        ),
    )
    suffix_entry_ids = frozenset(
        item.source_entry_id for item in suffix if item.source_entry_id is not None
    )
    manifests = tuple(
        item
        for item in old_dispatch.replay_manifest_cut.manifests
        if item.assistant_entry_id in suffix_entry_ids
    )
    scope = old_dispatch.replay_manifest_cut.scope
    manifest_cut = freeze_provider_replay_manifest_cut(
        session_id=identity.session_id,
        scope=scope,
        context_binding_revision_id=binding_revision_id,
        provider_input_through_sequence=identity.provider_input_through_sequence,
        manifests=manifests,
    )
    composite = context_fingerprint(
        "pulsara.canonical-provider-dispatch-read:v1",
        {
            "compile": facts.canonical_read_cut_fingerprint,
            "replay_manifest_cut": manifest_cut.cut_fingerprint,
        },
    )
    return FrozenCanonicalProviderDispatchRead(
        compile_snapshot=facts,
        replay_manifest_cut=manifest_cut,
        composite_fingerprint=composite,
    )


def rebase_compaction_dispatch_read_through_sequence(
    dispatch: FrozenCanonicalProviderDispatchRead,
    *,
    provider_input_through_sequence: int,
) -> FrozenCanonicalProviderDispatchRead:
    """Re-quote one synthetic scope cut after foreign-scope session writes.

    PostgreSQL entry sequence is session-global while canonical provider input
    is exact-scope.  A concurrently running child may therefore advance the
    next ROOT cut without adding any ROOT item or replay manifest.  This helper
    changes only that global cut coordinate; the caller must compare the whole
    rebuilt value to the authoritative reader result before using it.
    """

    facts = dispatch.compile_snapshot
    old_input = facts.canonical_input
    old_identity = old_input.identity
    if provider_input_through_sequence < old_identity.provider_input_through_sequence:
        raise ValueError("compaction dispatch cut cannot move backwards")
    identity_values = {
        "session_id": old_identity.session_id,
        "turn_id": old_identity.turn_id,
        "initial_entry_id": old_identity.initial_entry_id,
        "context_binding_revision_id": old_identity.context_binding_revision_id,
        "provider_input_through_sequence": provider_input_through_sequence,
        "conversation_scope_kind": old_identity.conversation_scope_kind,
        "scope_subagent_task_id": old_identity.scope_subagent_task_id,
    }
    identity = CanonicalModelInputIdentity(
        **identity_values,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            **identity_values
        ),
    )
    canonical_input = CanonicalModelInputSnapshot(
        identity=identity,
        items=old_input.items,
        canonical_utf8_bytes=old_input.canonical_utf8_bytes,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=old_input.items,
            canonical_utf8_bytes=old_input.canonical_utf8_bytes,
            closures=old_input.closures,
            late_outcomes=old_input.late_outcomes,
        ),
        closures=old_input.closures,
        late_outcomes=old_input.late_outcomes,
    )
    fact_values = {
        "canonical_input": canonical_input,
        "context_binding_fact": facts.context_binding_fact,
        "run_permission_snapshot": facts.run_permission_snapshot,
        "plan_workflow_fact": facts.plan_workflow_fact,
        "plan_handoff_fact": facts.plan_handoff_fact,
        "approved_plan_materialization_fact": (
            facts.approved_plan_materialization_fact
        ),
        "previous_turn_outcome_fact": facts.previous_turn_outcome_fact,
        "tool_observation_freshness_fact": (
            facts.tool_observation_freshness_fact
        ),
    }
    provisional_facts = FrozenCanonicalCompileSnapshot.__new__(
        FrozenCanonicalCompileSnapshot
    )
    for name, value in fact_values.items():
        object.__setattr__(provisional_facts, name, value)
    object.__setattr__(provisional_facts, "canonical_read_cut_fingerprint", "")
    rebased_facts = FrozenCanonicalCompileSnapshot(
        **fact_values,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional_facts
        ),
    )
    old_manifest = dispatch.replay_manifest_cut
    manifest_cut = freeze_provider_replay_manifest_cut(
        session_id=old_manifest.session_id,
        scope=old_manifest.scope,
        context_binding_revision_id=old_manifest.context_binding_revision_id,
        provider_input_through_sequence=provider_input_through_sequence,
        manifests=old_manifest.manifests,
    )
    composite = context_fingerprint(
        "pulsara.canonical-provider-dispatch-read:v1",
        {
            "compile": rebased_facts.canonical_read_cut_fingerprint,
            "replay_manifest_cut": manifest_cut.cut_fingerprint,
        },
    )
    return FrozenCanonicalProviderDispatchRead(
        compile_snapshot=rebased_facts,
        replay_manifest_cut=manifest_cut,
        composite_fingerprint=composite,
    )


def _synthetic_approved_plan_fact(
    fact: ApprovedPlanMaterializationFact | None,
    *,
    items: tuple[FrozenProviderInputItem, ...],
) -> ApprovedPlanMaterializationFact | None:
    if fact is None:
        return None
    pinned = next(
        (
            item
            for item in items
            if item.source_entry_id == fact.assistant_entry_id
            and any(call.tool_call_id == fact.tool_call_id for call in item.tool_calls)
        ),
        None,
    )
    disposition = (
        PlanApprovedMaterializationDisposition.PIN_EXISTING_CANONICAL_BLOCK
        if pinned is not None
        else PlanApprovedMaterializationDisposition.MATERIALIZE_REFERENCED_BLOCK
    )
    provisional = replace(
        fact,
        disposition=disposition,
        pinned_canonical_item_fingerprint=(
            None if pinned is None else provider_input_item_fingerprint(pinned)
        ),
        fact_fingerprint="",
    )
    return replace(
        provisional,
        fact_fingerprint=approved_plan_materialization_fingerprint(provisional),
    )


def freeze_compaction_source_view(
    *,
    canonical_read: FrozenCompactionCanonicalRead,
    compile_binding: ModelInputCompileBinding,
    compiled_result: FrozenProviderInputAppendCompileResult,
    predecessor_epoch_view: FrozenProviderInputEpochView | None,
) -> FrozenCompactionSourceView:
    """Combine existing carriers without creating another executable input."""

    compiled = compiled_result.compiled_input
    tools = compile_binding.tool_surface.tool_specs
    if compiled.tools != tools:
        raise CompactionPlanningError("compiled tools differ from the source binding")
    if compiled.compile_binding_fingerprint != compile_binding.binding_fingerprint:
        raise CompactionPlanningError("compiled input belongs to another target")
    if predecessor_epoch_view is None:
        compatibility = CompactionSourceCompatibility.EMPTY_COLD
    elif compiled_result.reset_reason is None:
        compatibility = CompactionSourceCompatibility.COMPATIBLE_APPEND
    else:
        compatibility = CompactionSourceCompatibility.PENDING_NON_COMPACTION_RESET

    if compatibility is CompactionSourceCompatibility.COMPATIBLE_APPEND:
        assert predecessor_epoch_view is not None
        prefix_count = len(predecessor_epoch_view.messages)
        if (
            predecessor_epoch_view.system_prompt != compiled.system_prompt
            or predecessor_epoch_view.tools != compiled.tools
            or compiled.messages[:prefix_count] != predecessor_epoch_view.messages
            or compiled_result.appended_message_count
            != len(compiled.messages) - prefix_count
        ):
            raise CompactionPlanningError(
                "compatible source view is not an exact append"
            )
        suffix = compiled.messages[prefix_count:]
        projection_logical_bytes = _logical_input_bytes(
            compiled.system_prompt, compiled.messages, compiled.tools
        )
        from pulsara_agent.conversation_kernel.compaction.contracts import (
            CompatibleAppendCompactionProjection,
        )

        projection = CompatibleAppendCompactionProjection(
            append_only_messages=suffix,
            final_estimate=compiled.final_estimate,
            logical_utf8_bytes=projection_logical_bytes,
        )
    else:
        projection_logical_bytes = _logical_input_bytes(
            compiled.system_prompt, compiled.messages, compiled.tools
        )
        projection = ColdRebuildCompactionProjection(
            system_prompt=compiled.system_prompt,
            full_messages=compiled.messages,
            final_estimate=compiled.final_estimate,
            logical_utf8_bytes=projection_logical_bytes,
        )
    canonical_range = canonical_read.safe_head_range
    working_values = {
        "post_base_item_count": len(canonical_range.ordered_items),
        "post_base_canonical_utf8_bytes": canonical_range.canonical_utf8_bytes,
        "continuity_epoch_logical_utf8_bytes": projection.logical_utf8_bytes,
        "resolved_hard_bound_set_fingerprint": (
            resolved_compaction_headroom_bounds()
            .resolved_hard_bound_set_fingerprint
        ),
    }
    working = CompactionPhysicalWorkingSetReport(**working_values)
    values = {
        "compatibility": compatibility,
        "canonical_dispatch_read": canonical_read.dispatch_read,
        "normal_compile_binding": compile_binding,
        "predecessor_epoch_view": predecessor_epoch_view,
        "provider_projection": projection,
        "physical_working_set": working,
    }
    return FrozenCompactionSourceView(
        **values,
        source_view_fingerprint=context_fingerprint(
            "pulsara.frozen-compaction-source-view.v1",
            {
                "compatibility": compatibility.value,
                "dispatch_read": canonical_read.dispatch_read.composite_fingerprint,
                "compile_binding": compile_binding.binding_fingerprint,
                "predecessor": (
                    None
                    if predecessor_epoch_view is None
                    else predecessor_epoch_view.semantic_prefix_fingerprint
                ),
                "projection": _compaction_projection_identity_digest(
                    projection, compile_binding, predecessor_epoch_view
                ),
                "working_set": _compaction_working_set_identity_digest(working),
            },
        ),
    )


def enumerate_complete_tool_groups(
    canonical_read: FrozenCompactionCanonicalRead,
) -> tuple[CompleteToolGroup, ...]:
    """Find whole assistant-call batches and their exact result/closure leaves."""

    items = canonical_read.dispatch_read.compile_snapshot.canonical_input.items
    results: dict[tuple[str, str], str] = {}
    closures = {
        (item.assistant_entry_id, item.tool_call_id): context_fingerprint(
            "pulsara.complete-tool-group-closure.v1",
            (
                item.assistant_entry_id,
                item.tool_call_id,
                item.closure_kind.value,
                item.target_provider_input_through_sequence,
            ),
        )
        for item in canonical_read.dispatch_read.compile_snapshot.canonical_input.closures
    }
    for item in items:
        if (
            item.item_kind
            in {
                FrozenProviderInputItemKind.TOOL_RESULT,
                FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
            }
            and item.tool_call_id is not None
            and item.tool_request_entry_id is not None
        ):
            key = (item.tool_request_entry_id, item.tool_call_id)
            if key in results:
                raise CompactionPlanningError(
                    "tool result attribution is duplicated"
                )
            results[key] = provider_input_item_fingerprint(item)
    groups: list[CompleteToolGroup] = []
    for item in items:
        if item.item_kind is not FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST:
            continue
        assert item.source_entry_id is not None
        assert item.source_entry_sequence is not None
        call_ids = tuple(call.tool_call_id for call in item.tool_calls)
        leaves = tuple(
            results.get((item.source_entry_id, call_id))
            or closures.get((item.source_entry_id, call_id))
            or ""
            for call_id in call_ids
        )
        if all(leaves):
            groups.append(
                CompleteToolGroup(
                    assistant_entry_id=item.source_entry_id,
                    assistant_entry_sequence=item.source_entry_sequence,
                    ordered_tool_call_ids=call_ids,
                    ordered_result_or_closure_fingerprints=leaves,
                )
            )
    return tuple(groups)


def freeze_tail_and_prefix(
    *,
    source_view: FrozenCompactionSourceView,
    complete_tool_groups: tuple[CompleteToolGroup, ...],
    retained_group_count: int,
) -> tuple[ProtectedTailSelectionFact, ProviderPrefixCutProof]:
    """Freeze one of the closed longest-suffix tail candidates."""

    if not 0 <= retained_group_count <= len(complete_tool_groups):
        raise ValueError("retained compaction group count is out of bounds")
    retained = (
        complete_tool_groups[-retained_group_count:]
        if retained_group_count
        else ()
    )
    messages = source_view.materialized_messages()
    placements = (
        source_view.canonical_dispatch_read.compile_snapshot.canonical_input.items
    )
    del placements
    if retained:
        earliest = retained[0]
        call_ids = earliest.ordered_tool_call_ids
        matches = tuple(
            index
            for index, message in enumerate(messages)
            if tuple(call.id for call in message.tool_calls) == call_ids
        )
        if len(matches) != 1:
            raise CompactionPlanningError(
                "retained assistant request has no unique message placement"
            )
        tail_start = matches[0]
        boundary = earliest.assistant_entry_sequence - 1
        earliest_id = earliest.assistant_entry_id
    else:
        tail_start = len(messages)
        boundary = source_view.exact_safe_canonical_head
        earliest_id = None
    tail_values = {
        "source_view_fingerprint": source_view.source_view_fingerprint,
        "retained_groups": retained,
        "earliest_retained_assistant_entry_id": earliest_id,
        "source_through_sequence": boundary,
        "protected_tail_message_start_index": tail_start,
    }
    tail = ProtectedTailSelectionFact(
        **tail_values,
        protected_tail_selection_fingerprint=context_fingerprint(
            "pulsara.protected-tail-selection.v1",
            {
                "source_view": source_view.source_view_fingerprint,
                "groups": tuple(item.fingerprint for item in retained),
                "earliest": earliest_id,
                "source_through_sequence": boundary,
                "start": tail_start,
            },
        ),
    )
    prefix = messages[:tail_start]
    prefix_fingerprint = compaction_summary_message_prefix_fingerprint(prefix)
    proof = ProviderPrefixCutProof(
        source_view_fingerprint=source_view.source_view_fingerprint,
        summary_prefix_message_count=len(prefix),
        summary_prefix_messages_fingerprint=prefix_fingerprint,
        source_through_sequence=boundary,
        protected_tail_selection_fingerprint=(
            tail.protected_tail_selection_fingerprint
        ),
        proof_fingerprint=context_fingerprint(
            "pulsara.provider-prefix-cut-proof.v1",
            {
                "source_view": source_view.source_view_fingerprint,
                "message_count": len(prefix),
                "messages": prefix_fingerprint,
                "through": boundary,
                "tail": tail.protected_tail_selection_fingerprint,
            },
        ),
    )
    return tail, proof


def select_recent_human_messages(
    *,
    canonical_read: FrozenCompactionCanonicalRead,
    source_through_sequence: int,
    policy: ResolvedCompactionPolicy,
) -> tuple[RecentHumanMessageProof, ...]:
    items = tuple(
        item
        for item in canonical_read.dispatch_read.compile_snapshot.canonical_input.items
        if item.item_kind is FrozenProviderInputItemKind.USER
        and item.input_origin
        in {
            CanonicalInputOriginKind.HUMAN_MESSAGE,
            CanonicalInputOriginKind.HUMAN_STEER,
        }
        and item.source_entry_id is not None
        and item.source_entry_sequence is not None
        and item.source_entry_sequence <= source_through_sequence
    )
    selected: list[RecentHumanMessageProof] = []
    aggregate = 0
    for item in reversed(items):
        size = len(item.text.encode("utf-8"))
        if aggregate + size > policy.maximum_recent_human_utf8_bytes:
            if not selected:
                return ()
            break
        selected.append(
            RecentHumanMessageProof(
                entry_id=item.source_entry_id,
                entry_sequence=item.source_entry_sequence,
                text=item.text,
            )
        )
        aggregate += size
        if len(selected) == policy.maximum_recent_human_messages:
            break
    return tuple(reversed(selected))


def should_trigger_compaction(
    *,
    source_view: FrozenCompactionSourceView,
    policy: ResolvedCompactionPolicy,
    force: bool,
) -> bool:
    if force:
        return True
    budget = source_view.normal_compile_binding.effective_input_budget_tokens
    estimate = source_view.provider_projection.final_estimate.total_input_tokens
    token_trigger = estimate >= int(budget * policy.auto_trigger_ratio)
    working = source_view.physical_working_set
    headroom_trigger = crosses_compaction_resource_headroom(
        post_base_item_count=working.post_base_item_count,
        post_base_canonical_utf8_bytes=(
            working.post_base_canonical_utf8_bytes
        ),
        continuity_epoch_logical_utf8_bytes=(
            working.continuity_epoch_logical_utf8_bytes
        ),
    )
    return token_trigger or headroom_trigger


def crosses_compaction_resource_headroom(
    *,
    post_base_item_count: int,
    post_base_canonical_utf8_bytes: int,
    continuity_epoch_logical_utf8_bytes: int,
) -> bool:
    bounds = resolved_compaction_headroom_bounds()
    return (
        post_base_item_count >= bounds.soft_canonical_item_limit
        or post_base_canonical_utf8_bytes
        >= bounds.soft_canonical_utf8_byte_limit
        or continuity_epoch_logical_utf8_bytes
        >= bounds.soft_epoch_logical_utf8_byte_limit
    )


def validate_compaction_reclaim(
    *,
    source_tokens: int,
    successor_tokens: int,
    hard_input_budget_tokens: int,
    policy: ResolvedCompactionPolicy,
    force: bool,
    enforce_soft_target: bool,
) -> int:
    """Validate the one closed reclaim rule for active and idle adoption.

    ``force`` bypasses only the proactive post-target ratio.  It never makes a
    non-reclaiming snapshot useful and never bypasses the minimum reclaim,
    except when the old exact input already crossed the hard provider boundary.
    """

    if min(source_tokens, successor_tokens, hard_input_budget_tokens) < 0:
        raise ValueError("compaction token quote is negative")
    if successor_tokens > hard_input_budget_tokens:
        raise CompactionPlanningError(
            "compaction successor exceeds its hard model budget"
        )
    reclaim = source_tokens - successor_tokens
    if reclaim <= 0:
        raise CompactionPlanningError(
            "compaction successor does not reclaim context"
        )
    if (
        reclaim < policy.minimum_reclaim_tokens
        and source_tokens <= hard_input_budget_tokens
    ):
        raise CompactionPlanningError(
            "compaction successor does not reclaim enough context"
        )
    if (
        enforce_soft_target
        and not force
        and successor_tokens
        > int(hard_input_budget_tokens * policy.post_compaction_target_ratio)
    ):
        raise CompactionPlanningError(
            "compaction successor exceeds its post target"
        )
    return reclaim


def estimate_unavoidable_compaction_successor_tokens(
    *,
    source_view: FrozenCompactionSourceView,
    tail: ProtectedTailSelectionFact,
    recent_user_messages: tuple[RecentHumanMessageProof, ...],
    deadline_monotonic: float,
) -> int:
    """Quote a conservative lower bound before opening the summary provider.

    Protected canonical human messages cannot be degraded by the compiler.  A
    candidate whose minimal non-empty snapshot plus those exact messages is
    already over the post target can therefore never become admissible,
    regardless of how concise the summary is or how aggressively optional
    runtime observations and ToolResults degrade.  Rejecting that candidate
    before provider open lets the longest-suffix planner continue from
    ``3 -> 2 -> 1 -> 0`` without inventing a maximum summary size.

    This deliberately omits SYSTEM, tools, replaceable observations,
    assistant messages and ToolResults.  It is a lower-bound proof, not a
    substitute for the final exact cold-epoch assembly.
    """

    if deadline_monotonic <= 0:
        raise ValueError("compaction candidate deadline is invalid")
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("compaction planning deadline expired")
    if tail.source_view_fingerprint != source_view.source_view_fingerprint:
        raise ValueError("compaction tail belongs to another source view")

    canonical = source_view.canonical_dispatch_read.compile_snapshot.canonical_input
    unavoidable_messages = tuple(
        LLMMessage.user(item.text)
        for item in canonical.items
        if item.item_kind is FrozenProviderInputItemKind.USER
        and item.source_entry_sequence is not None
        and item.source_entry_sequence > tail.source_through_sequence
    )
    minimal_summary = freeze_compaction_summary_output(
        "x",
        maximum_utf8_bytes=1,
    )
    carrier = build_compaction_snapshot_carrier(
        summary=minimal_summary,
        recent_user_messages=tuple(item.text for item in recent_user_messages),
    )
    messages = (LLMMessage.user(carrier.body.decode("utf-8")), *unavoidable_messages)
    estimator = source_view.normal_compile_binding.estimator

    def checkpoint() -> None:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("compaction planning deadline expired")

    cooperative = getattr(estimator, "estimate_frozen_input_cooperative", None)
    if callable(cooperative):
        estimate = cooperative(
            system_prompt="",
            messages=messages,
            tools=(),
            checkpoint=checkpoint,
        )
    else:
        checkpoint()
        estimate = estimator.estimate_frozen_input(
            system_prompt="",
            messages=messages,
            tools=(),
        )
        checkpoint()
    return estimate.total_input_tokens


def _logical_input_bytes(
    system_prompt: str,
    messages: tuple[LLMMessage, ...],
    tools: tuple[FrozenToolSpec, ...],
) -> int:
    return provider_input_logical_utf8_bytes(
        system_prompt=system_prompt,
        tools=tools,
        messages=messages,
    )


__all__ = [
    "CompactionPlanningError",
    "crosses_compaction_resource_headroom",
    "enumerate_complete_tool_groups",
    "estimate_unavoidable_compaction_successor_tokens",
    "freeze_compaction_source_view",
    "freeze_tail_and_prefix",
    "select_recent_human_messages",
    "should_trigger_compaction",
]
