"""Pure one-cut planning for Round 5B context compaction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import monotonic

from pulsara_agent.conversation_kernel.compaction.contracts import (
    FrozenRetainedHistoricalRequest,
    ColdRebuildCompactionProjection,
    CompactionActiveRequestLocation,
    CompactionContinuationMode,
    CompactionPhysicalWorkingSetReport,
    CompactionSourceCompatibility,
    CompactionSnapshotCarrier,
    CompactionTargetBranch,
    CompleteToolGroup,
    FrozenCompactionActiveRequest,
    FrozenCompactionCanonicalRead,
    FrozenCompactionSummary,
    FrozenCompactionSourceView,
    ProtectedTailSelectionFact,
    ProviderPrefixCutProof,
    RecentHumanMessageProof,
    ResolvedCompactionPolicy,
    _compaction_projection_identity_digest,
    _compaction_working_set_identity_digest,
    compaction_summary_message_prefix_fingerprint,
    freeze_compaction_canonical_range,
    resolved_compaction_headroom_bounds,
    provider_input_item_canonical_expanded_bytes,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    compaction_snapshot_canonical_expanded_bytes,
    display_json,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMContentPart,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    content_has_image,
)
from pulsara_agent.model_input.lowering import (
    image_reference_part,
    lower_retained_request_content,
)
from pulsara_agent.llm.request import FrozenProviderWireInputQuote
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
    FrozenModelInputSemanticProjection,
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
    FrozenProviderInputAppendSemanticProjection,
    FrozenProviderInputEpochView,
    PROVIDER_MESSAGE_LOWERING_CONTRACT,
    provider_input_logical_bytes,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    freeze_provider_replay_manifest_cut,
)
from pulsara_agent.primitives.context import context_fingerprint


class CompactionPlanningError(RuntimeError):
    """A body-free deterministic compaction planning failure."""


class CompactionReclaimUnavailable(CompactionPlanningError):
    """The candidate is valid but cannot reclaim enough context to adopt."""


class NoSafeCompactionSummaryPrefix(CompactionPlanningError):
    """The exact source cut contains no complete provider-safe summary prefix."""


_DESTINATION_PROJECTION_NOTICE = (
    "This is a lossy Runtime-authored source for a destination-side compaction "
    "call. User and assistant text is quoted exactly. Tool evidence is advisory, "
    "not live replay."
)

TEXT_ONLY_IMAGE_OMISSION_TEXT = "[Image omitted]"


@dataclass(frozen=True, slots=True)
class DestinationToolEvidence:
    request_entry_id: str
    tool_call_id: str
    name: str
    result_status: str
    result_entry_id: str | None
    result_entry_sequence: int | None
    outcome_ordinal: int
    result_content: tuple[LLMContentPart, ...] | None

    @property
    def key(self) -> tuple[str, str]:
        return self.request_entry_id, self.tool_call_id


@dataclass(frozen=True, slots=True)
class DestinationDialogueEntry:
    role: str
    item_kind: FrozenProviderInputItemKind
    input_origin: CanonicalInputOriginKind | None
    content: tuple[LLMContentPart, ...]
    requested_tools: tuple[DestinationToolEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class RecentDialogueUnit:
    turn_id: str
    source_entry_sequences: tuple[int, ...]
    entries: tuple[DestinationDialogueEntry, ...]

    def __post_init__(self) -> None:
        if (
            not self.turn_id
            or not self.source_entry_sequences
            or not self.entries
            or not any(item.role == "user" for item in self.entries)
        ):
            raise ValueError("destination dialogue unit is not a complete turn")


@dataclass(frozen=True, slots=True)
class DestinationDialogueProjectionPlan:
    units: tuple[RecentDialogueUnit, ...]
    prior_handoff: (
        tuple[
            str,
            tuple[FrozenRetainedHistoricalRequest, ...],
            tuple[FrozenRetainedHistoricalRequest, ...],
        ]
        | None
    )


@dataclass(frozen=True, slots=True)
class DestinationDialogueProjection:
    units: tuple[RecentDialogueUnit, ...]
    prior_handoff: (
        tuple[
            str,
            tuple[FrozenRetainedHistoricalRequest, ...],
            tuple[FrozenRetainedHistoricalRequest, ...],
        ]
        | None
    )
    retained_result_keys: frozenset[tuple[str, str]]
    content: tuple[LLMContentPart, ...]

    @property
    def eligible_evidence(self) -> tuple[DestinationToolEvidence, ...]:
        return tuple(
            evidence
            for unit in self.units
            for entry in unit.entries
            for evidence in entry.requested_tools
            if evidence.result_content is not None
            and evidence.key not in self.retained_result_keys
        )


def compaction_effective_history_has_image(
    canonical_read: FrozenCompactionCanonicalRead,
) -> bool:
    """Return whether the effective history, excluding the exact active request, has images."""

    return provider_dispatch_effective_history_has_image(
        canonical_read.dispatch_read
    )


def provider_dispatch_effective_history_has_image(
    dispatch_read: FrozenCanonicalProviderDispatchRead,
) -> bool:
    """Inspect typed history while excluding the current exact request."""

    canonical = dispatch_read.compile_snapshot.canonical_input
    active_entry_id = canonical.identity.initial_entry_id
    for item in canonical.items:
        if item.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT:
            carrier = item.content
            if not isinstance(carrier, CompactionSnapshotCarrier):
                raise TypeError("canonical snapshot item lacks its typed carrier")
            active = carrier.active_request
            if (
                active is not None
                and active.entry_id != active_entry_id
                and active.content is not None
                and content_has_image(active.content.parts)
            ):
                return True
            if any(
                content_has_image(request.content.parts)
                for request in (
                    *carrier.recent_human_requests,
                    *carrier.retained_historical_requests,
                )
            ):
                return True
            continue
        if item.source_entry_id == active_entry_id:
            continue
        if isinstance(item.content, tuple) and content_has_image(item.content):
            return True
    return False


def recent_human_window_has_image(
    recent: tuple[RecentHumanMessageProof, ...],
) -> bool:
    return any(content_has_image(item.content.parts) for item in recent)


def project_prompt_content_for_text_only_handover(
    content: FrozenPromptContent,
) -> FrozenPromptContent:
    """Apply D4's single history projection P while preserving part order."""

    return FrozenPromptContent(
        tuple(
            LLMTextPart(TEXT_ONLY_IMAGE_OMISSION_TEXT)
            if isinstance(part, LLMImagePart)
            else part
            for part in content.parts
        )
    )


def project_retained_request_for_text_only_handover(
    request: FrozenRetainedHistoricalRequest,
) -> FrozenRetainedHistoricalRequest:
    return replace(
        request,
        content=project_prompt_content_for_text_only_handover(request.content),
    )


def project_destination_dialogue_for_text_only_handover(
    projection: DestinationDialogueProjection,
) -> DestinationDialogueProjection:
    return replace(
        projection,
        content=tuple(
            LLMTextPart(TEXT_ONLY_IMAGE_OMISSION_TEXT)
            if isinstance(part, LLMImagePart)
            else part
            for part in projection.content
        ),
    )


def project_destination_dialogue_plan_for_text_only_handover(
    plan: DestinationDialogueProjectionPlan,
) -> DestinationDialogueProjectionPlan:
    """Apply P to all candidate dialogue and ToolResult evidence before selection."""

    def project_parts(parts: tuple[LLMContentPart, ...]) -> tuple[LLMContentPart, ...]:
        return tuple(
            LLMTextPart(TEXT_ONLY_IMAGE_OMISSION_TEXT)
            if isinstance(part, LLMImagePart)
            else part
            for part in parts
        )

    return replace(
        plan,
        units=tuple(
            replace(
                unit,
                entries=tuple(
                    replace(
                        entry,
                        content=project_parts(entry.content),
                        requested_tools=tuple(
                            replace(
                                evidence,
                                result_content=(
                                    None
                                    if evidence.result_content is None
                                    else project_parts(evidence.result_content)
                                ),
                            )
                            for evidence in entry.requested_tools
                        ),
                    )
                    for entry in unit.entries
                ),
            )
            for unit in plan.units
        ),
    )


def project_compaction_read_for_text_only_handover(
    canonical_read: FrozenCompactionCanonicalRead,
) -> FrozenCompactionCanonicalRead:
    """Build the process-local Tier-3 P view of one authoritative canonical cut.

    The exact active request of a running turn remains untouched.  Every historical
    image occurrence becomes the ordinary text part required by D4.  A terminal
    source has no active request, so its initial request is historical too.  The
    resulting value is used only to compile B's source call; canonical settlement
    continues to use the unprojected reader value.
    """

    old_dispatch = canonical_read.dispatch_read
    old_facts = old_dispatch.compile_snapshot
    old_input = old_facts.canonical_input
    active_entry_id = (
        old_input.identity.initial_entry_id
        if canonical_read.turn_status == "RUNNING"
        else None
    )

    def project_carrier(carrier: CompactionSnapshotCarrier) -> CompactionSnapshotCarrier:
        active = carrier.active_request
        if (
            active is not None
            and active.entry_id != active_entry_id
            and active.content is not None
        ):
            active = replace(
                active,
                content=project_prompt_content_for_text_only_handover(active.content),
            )
        summary = FrozenCompactionSummary(
            body=carrier.earlier_context_summary,
            body_utf8_bytes=len(carrier.earlier_context_summary.encode("utf-8")),
            body_digest=context_fingerprint(
                "pulsara.frozen-compaction-summary.v2-guided-freeform",
                carrier.earlier_context_summary,
            ),
        )
        return build_compaction_snapshot_carrier(
            summary=summary,
            recent_human_requests=tuple(
                project_retained_request_for_text_only_handover(item)
                for item in carrier.recent_human_requests
            ),
            continuation_mode=carrier.continuation_mode,
            active_request=active,
            retained_historical_requests=tuple(
                project_retained_request_for_text_only_handover(item)
                for item in carrier.retained_historical_requests
            ),
        )

    items: list[FrozenProviderInputItem] = []
    for item in old_input.items:
        if item.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT:
            carrier = item.content
            if not isinstance(carrier, CompactionSnapshotCarrier):
                raise TypeError("canonical snapshot item lacks its typed carrier")
            items.append(replace(item, content=project_carrier(carrier)))
            continue
        if item.source_entry_id == active_entry_id:
            items.append(item)
            continue
        if isinstance(item.content, tuple) and content_has_image(item.content):
            items.append(
                replace(
                    item,
                    content=project_prompt_content_for_text_only_handover(
                        FrozenPromptContent(item.content)
                    ).parts,
                )
            )
            continue
        items.append(item)
    projected_items = tuple(items)
    canonical_bytes = sum(
        item.content.canonical_expanded_bytes
        if item.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
        else provider_input_item_canonical_expanded_bytes(item)
        for item in projected_items
    )
    projected_input = CanonicalModelInputSnapshot(
        identity=old_input.identity,
        items=projected_items,
        canonical_expanded_bytes=canonical_bytes,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=old_input.identity,
            items=projected_items,
            canonical_expanded_bytes=canonical_bytes,
            closures=old_input.closures,
            late_outcomes=old_input.late_outcomes,
        ),
        closures=old_input.closures,
        late_outcomes=old_input.late_outcomes,
    )
    fact_values = {
        "canonical_input": projected_input,
        "context_binding_fact": old_facts.context_binding_fact,
        "run_permission_snapshot": old_facts.run_permission_snapshot,
        "plan_workflow_fact": old_facts.plan_workflow_fact,
        "plan_handoff_fact": old_facts.plan_handoff_fact,
        "approved_plan_materialization_fact": (
            old_facts.approved_plan_materialization_fact
        ),
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
    projected_facts = FrozenCanonicalCompileSnapshot(
        **fact_values,
        canonical_read_cut_fingerprint=canonical_compile_snapshot_fingerprint(
            provisional_facts
        ),
    )
    manifest_cut = old_dispatch.replay_manifest_cut
    projected_dispatch = FrozenCanonicalProviderDispatchRead(
        compile_snapshot=projected_facts,
        replay_manifest_cut=manifest_cut,
        composite_fingerprint=context_fingerprint(
            "pulsara.canonical-provider-dispatch-read:v1",
            {
                "compile": projected_facts.canonical_read_cut_fingerprint,
                "replay_manifest_cut": manifest_cut.cut_fingerprint,
            },
        ),
    )
    projected_range = freeze_compaction_canonical_range(
        scope=canonical_read.scope,
        effective_materialization_lineage_floor=(
            canonical_read.lineage_base.effective_materialization_lineage_floor
        ),
        source_through_sequence=(
            old_input.identity.provider_input_through_sequence
        ),
        ordered_items=projected_items,
        closures=old_input.closures,
        late_outcomes=old_input.late_outcomes,
    )
    return FrozenCompactionCanonicalRead(
        scope=canonical_read.scope,
        turn_status=canonical_read.turn_status,
        dispatch_read=projected_dispatch,
        lineage_base=canonical_read.lineage_base,
        safe_head_range=projected_range,
    )


def freeze_destination_dialogue_projection_plan(
    *,
    canonical_read: FrozenCompactionCanonicalRead,
    active_request: FrozenCompactionActiveRequest | None,
) -> DestinationDialogueProjectionPlan:
    """Freeze exact historical turn units from the current effective lineage."""

    canonical = canonical_read.dispatch_read.compile_snapshot.canonical_input
    if active_request is None:
        if canonical_read.turn_status not in {"COMPLETED", "INTERRUPTED"}:
            raise CompactionPlanningError(
                "destination projection lacks one exact active request"
            )
        active_entry_id = None
    else:
        if (
            canonical_read.turn_status != "RUNNING"
            or active_request.location
            is not CompactionActiveRequestLocation.SNAPSHOT_EXACT
            or active_request.content is None
            or active_request.entry_id != canonical.identity.initial_entry_id
        ):
            raise CompactionPlanningError(
                "destination projection lacks one exact active request"
            )
        active_entry_id = active_request.entry_id
    outcome_by_call: dict[tuple[str, str], DestinationToolEvidence] = {}
    outcome_ordinal = 0
    for item in canonical_read.safe_head_range.ordered_items:
        if item.item_kind not in {
            FrozenProviderInputItemKind.TOOL_RESULT,
            FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        }:
            continue
        assert item.tool_request_entry_id is not None
        assert item.tool_call_id is not None
        assert item.tool_result_context is not None
        outcome_ordinal += 1
        key = (item.tool_request_entry_id, item.tool_call_id)
        if key in outcome_by_call:
            raise CompactionPlanningError(
                "destination projection tool outcome is duplicated"
            )
        outcome_by_call[key] = DestinationToolEvidence(
            request_entry_id=item.tool_request_entry_id,
            tool_call_id=item.tool_call_id,
            name="",
            result_status=item.tool_result_context.result_state.lower(),
            result_entry_id=item.source_entry_id,
            result_entry_sequence=item.source_entry_sequence,
            outcome_ordinal=outcome_ordinal,
            result_content=(
                item.content if isinstance(item.content, tuple) else None
            ),
        )
    for closure in canonical_read.safe_head_range.closures:
        key = (closure.assistant_entry_id, closure.tool_call_id)
        if key in outcome_by_call:
            continue
        outcome_ordinal += 1
        outcome_by_call[key] = DestinationToolEvidence(
            request_entry_id=closure.assistant_entry_id,
            tool_call_id=closure.tool_call_id,
            name="",
            result_status=closure.closure_kind.value,
            result_entry_id=None,
            result_entry_sequence=None,
            outcome_ordinal=outcome_ordinal,
            result_content=None,
        )

    grouped: dict[str, list[FrozenProviderInputItem]] = {}
    turn_order: list[str] = []
    for item in canonical_read.safe_head_range.ordered_items:
        if (
            (active_entry_id is not None and item.source_entry_id == active_entry_id)
            or item.item_kind
            in {
                FrozenProviderInputItemKind.TOOL_RESULT,
                FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
                FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE,
            }
            or item.source_turn_id is None
        ):
            continue
        if item.source_turn_id not in grouped:
            grouped[item.source_turn_id] = []
            turn_order.append(item.source_turn_id)
        grouped[item.source_turn_id].append(item)

    units: list[RecentDialogueUnit] = []
    request_kinds = {
        FrozenProviderInputItemKind.USER,
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        FrozenProviderInputItemKind.PLAN_CONTINUATION,
        FrozenProviderInputItemKind.INTER_AGENT_MESSAGE,
    }
    request_ordinal = 0
    for turn_id in turn_order:
        items = grouped[turn_id]
        entries: list[DestinationDialogueEntry] = []
        sequences: list[int] = []
        for item in items:
            assert item.source_entry_sequence is not None
            sequences.append(item.source_entry_sequence)
            if item.item_kind in request_kinds:
                entries.append(
                    DestinationDialogueEntry(
                        "user", item.item_kind, item.input_origin, item.content
                    )
                )
            elif item.item_kind is FrozenProviderInputItemKind.ASSISTANT:
                entries.append(
                    DestinationDialogueEntry(
                        "assistant", item.item_kind, None, item.content
                    )
                )
            elif item.item_kind is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST:
                assert item.source_entry_id is not None
                evidence: list[DestinationToolEvidence] = []
                for call in item.tool_calls:
                    request_ordinal += 1
                    outcome = outcome_by_call.get(
                        (item.source_entry_id, call.tool_call_id)
                    )
                    if outcome is None:
                        outcome = DestinationToolEvidence(
                            request_entry_id=item.source_entry_id,
                            tool_call_id=call.tool_call_id,
                            name=call.tool_name,
                            result_status="unknown",
                            result_entry_id=None,
                            result_entry_sequence=None,
                            outcome_ordinal=request_ordinal,
                            result_content=None,
                        )
                    else:
                        outcome = replace(
                            outcome,
                            name=call.tool_name,
                            outcome_ordinal=request_ordinal,
                        )
                    evidence.append(outcome)
                entries.append(
                    DestinationDialogueEntry(
                        "assistant",
                        item.item_kind,
                        None,
                        item.content,
                        tuple(evidence),
                    )
                )
        if entries and any(item.role == "user" for item in entries):
            units.append(RecentDialogueUnit(turn_id, tuple(sequences), tuple(entries)))

    prior_handoff = None
    prior = canonical_read.snapshot_carrier
    if prior is not None:
        prior_handoff = (
            prior.earlier_context_summary,
            prior.recent_human_requests,
            prior.retained_historical_requests,
        )
    return DestinationDialogueProjectionPlan(tuple(units), prior_handoff)


def enumerate_destination_backbone_projections(
    plan: DestinationDialogueProjectionPlan,
) -> tuple[DestinationDialogueProjection, ...]:
    """Enumerate full history through empty suffix without a trial cap."""

    candidates: list[DestinationDialogueProjection] = []
    if plan.prior_handoff is not None:
        candidates.append(
            _render_destination_projection(
                units=plan.units,
                prior_handoff=plan.prior_handoff,
                retained_result_keys=frozenset(),
            )
        )
    for start in range(0, len(plan.units) + 1):
        candidates.append(
            _render_destination_projection(
                units=plan.units[start:],
                prior_handoff=None,
                retained_result_keys=frozenset(),
            )
        )
    return tuple(candidates)


def retain_destination_tool_evidence(
    projection: DestinationDialogueProjection,
    evidence: DestinationToolEvidence,
) -> DestinationDialogueProjection:
    if evidence not in projection.eligible_evidence:
        raise ValueError("destination tool evidence is not eligible")
    return _render_destination_projection(
        units=projection.units,
        prior_handoff=projection.prior_handoff,
        retained_result_keys=(projection.retained_result_keys | {evidence.key}),
    )


def _render_destination_projection(
    *,
    units: tuple[RecentDialogueUnit, ...],
    prior_handoff: tuple[
        str,
        tuple[FrozenRetainedHistoricalRequest, ...],
        tuple[FrozenRetainedHistoricalRequest, ...],
    ]
    | None,
    retained_result_keys: frozenset[tuple[str, str]],
) -> DestinationDialogueProjection:
    available_keys = {
        evidence.key
        for unit in units
        for entry in unit.entries
        for evidence in entry.requested_tools
        if evidence.result_content is not None
    }
    if not retained_result_keys.issubset(available_keys):
        raise ValueError("retained evidence escaped its dialogue suffix")
    rendered_parts: list[LLMContentPart] = [
        LLMTextPart(
            display_json(
                {
                    "projection_notice": _DESTINATION_PROJECTION_NOTICE,
                    "prior_handoff": (
                        None
                        if prior_handoff is None
                        else {"earlier_context_summary": prior_handoff[0]}
                    ),
                }
            )
        )
    ]

    def append_section(
        *,
        section: str,
        role: str,
        content: tuple[LLMContentPart, ...],
        metadata: dict[str, object] | None = None,
    ) -> None:
        value: dict[str, object] = {"role": role, "section": section}
        if metadata:
            value.update(metadata)
        rendered_parts.append(
            LLMTextPart(
                "\n[PULSARA_RETAINED_CONTENT " + display_json(value) + "]\n"
            )
        )
        for part in content:
            if isinstance(part, LLMTextPart):
                rendered_parts.append(LLMTextPart(display_json(part.text)))
            else:
                rendered_parts.extend((image_reference_part(part), part))
        rendered_parts.append(LLMTextPart("\n[/PULSARA_RETAINED_CONTENT]\n"))

    if prior_handoff is not None:
        for request in prior_handoff[1]:
            append_section(
                section="prior_recent",
                role="user",
                content=lower_retained_request_content(request),
            )
        for request in prior_handoff[2]:
            append_section(
                section="prior_historical",
                role="user",
                content=lower_retained_request_content(request),
            )
    for turn_index, unit in enumerate(units):
        for entry in unit.entries:
            tools: list[dict[str, object]] = []
            for evidence in entry.requested_tools:
                tool: dict[str, object] = {
                    "name": evidence.name,
                    "result_status": evidence.result_status,
                }
                if evidence.key in retained_result_keys:
                    assert evidence.result_content is not None
                    tool["retained_result"] = True
                else:
                    tool["result_omitted"] = True
                tools.append(tool)
            if entry.role == "user":
                request = FrozenRetainedHistoricalRequest(
                    item_kind=entry.item_kind,
                    input_origin=entry.input_origin,
                    content=FrozenPromptContent(entry.content),
                )
                content = lower_retained_request_content(request)
            else:
                content = entry.content
            append_section(
                section="dialogue",
                role=entry.role,
                content=content,
                metadata={
                    "turn_index": turn_index,
                    **({"requested_tools": tools} if tools else {}),
                },
            )
            for evidence in entry.requested_tools:
                if evidence.key not in retained_result_keys:
                    continue
                assert evidence.result_content is not None
                append_section(
                    section="tool_evidence",
                    role="tool",
                    content=evidence.result_content,
                    metadata={
                        "turn_index": turn_index,
                        "name": evidence.name,
                        "tool_call_id": evidence.tool_call_id,
                        "result_status": evidence.result_status,
                    },
                )
    return DestinationDialogueProjection(
        units=units,
        prior_handoff=prior_handoff,
        retained_result_keys=retained_result_keys,
        content=tuple(rendered_parts),
    )


def build_synthetic_compaction_dispatch_read(
    *,
    canonical_read: FrozenCompactionCanonicalRead,
    source_through_sequence: int,
    snapshot_id: str,
    binding_revision_id: str,
    binding_revision_ordinal: int,
    snapshot_carrier: CompactionSnapshotCarrier,
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
        or snapshot_content_size != len(snapshot_carrier.body)
        or snapshot_content_codec != "utf-8"
        or not snapshot_content_digest.startswith("sha256:")
    ):
        raise ValueError("synthetic compaction snapshot identity is invalid")
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
        content=snapshot_carrier,
    )
    items = (snapshot_item, *suffix)
    request_ids = frozenset(
        item.source_entry_id
        for item in suffix
        if item.item_kind is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST
        and item.source_entry_id is not None
    )
    closures = tuple(
        item for item in old_input.closures if item.assistant_entry_id in request_ids
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
    # CanonicalModelInputSnapshot.canonical_expanded_bytes is the bounded reader
    # hydration quote: decoded entry/result text bytes, not the larger stable
    # semantic-leaf encoding used by compaction range fingerprints.  Mirror
    # that reader contract exactly so dry and post-FULL cuts can compare equal.
    canonical_bytes = compaction_snapshot_canonical_expanded_bytes(
        snapshot_carrier.body
    ) + sum(provider_input_item_canonical_expanded_bytes(item) for item in suffix)
    canonical_input = CanonicalModelInputSnapshot(
        identity=identity,
        items=items,
        canonical_expanded_bytes=canonical_bytes,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=items,
            canonical_expanded_bytes=canonical_bytes,
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
        fact_fingerprint=context_binding_compile_fact_fingerprint(provisional_binding),
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
        "tool_observation_freshness_fact": (old_facts.tool_observation_freshness_fact),
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
        canonical_expanded_bytes=old_input.canonical_expanded_bytes,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=old_input.items,
            canonical_expanded_bytes=old_input.canonical_expanded_bytes,
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
        "tool_observation_freshness_fact": (facts.tool_observation_freshness_fact),
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
    semantic_projection: FrozenProviderInputAppendSemanticProjection,
    predecessor_epoch_view: FrozenProviderInputEpochView | None,
    provider_wire_quote: FrozenProviderWireInputQuote,
    producer_wire_api: str,
) -> FrozenCompactionSourceView:
    """Combine existing carriers without creating another executable input."""

    compiled = semantic_projection.projected_input
    if provider_wire_quote.wire_api != producer_wire_api:
        raise CompactionPlanningError("wire quote belongs to another wire API")
    tools = compile_binding.tool_surface.tool_specs
    if compiled.tools != tools:
        raise CompactionPlanningError("compiled tools differ from the source binding")
    if compiled.compile_binding_fingerprint != compile_binding.binding_fingerprint:
        raise CompactionPlanningError("compiled input belongs to another target")
    canonical = canonical_read.dispatch_read.compile_snapshot.canonical_input
    if (
        compiled.canonical_input_identity != canonical.identity
        or semantic_projection.canonical_frontier.through_sequence
        != canonical.identity.provider_input_through_sequence
        or semantic_projection.canonical_frontier.latest_context_binding_revision_id
        != canonical.identity.context_binding_revision_id
        or semantic_projection.canonical_frontier.ordered_item_fingerprints
        != tuple(provider_input_item_fingerprint(item) for item in canonical.items)
    ):
        raise CompactionPlanningError(
            "semantic projection differs from the frozen canonical cut"
        )
    if predecessor_epoch_view is None:
        compatibility = CompactionSourceCompatibility.EMPTY_COLD
    else:
        compatibility = CompactionSourceCompatibility.COMPATIBLE_APPEND

    if compatibility is CompactionSourceCompatibility.COMPATIBLE_APPEND:
        assert predecessor_epoch_view is not None
        prefix_count = len(predecessor_epoch_view.messages)
        if (
            predecessor_epoch_view.system_prompt != compiled.system_prompt
            or predecessor_epoch_view.tools != compiled.tools
            or compiled.messages[:prefix_count] != predecessor_epoch_view.messages
            or semantic_projection.appended_message_count
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
            logical_bytes=projection_logical_bytes,
        )
    else:
        projection_logical_bytes = _logical_input_bytes(
            compiled.system_prompt, compiled.messages, compiled.tools
        )
        projection = ColdRebuildCompactionProjection(
            system_prompt=compiled.system_prompt,
            full_messages=compiled.messages,
            final_estimate=compiled.final_estimate,
            logical_bytes=projection_logical_bytes,
        )
    canonical_input = canonical_read.dispatch_read.compile_snapshot.canonical_input
    selected_items = canonical_input.items
    working_values = {
        "selected_item_count": len(selected_items),
        "selected_canonical_expanded_bytes": canonical_input.canonical_expanded_bytes,
        "continuity_epoch_logical_bytes": projection.logical_bytes,
        "resolved_hard_bound_set_fingerprint": (
            resolved_compaction_headroom_bounds().resolved_hard_bound_set_fingerprint
        ),
    }
    working = CompactionPhysicalWorkingSetReport(**working_values)
    values = {
        "compatibility": compatibility,
        "canonical_dispatch_read": canonical_read.dispatch_read,
        "normal_compile_binding": compile_binding,
        "predecessor_epoch_view": predecessor_epoch_view,
        "provider_projection": projection,
        "provider_wire_quote": provider_wire_quote,
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
                raise CompactionPlanningError("tool result attribution is duplicated")
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
    source_projection: FrozenModelInputSemanticProjection | None = None,
    summary_prefix_message_count: int | None = None,
    deadline_monotonic: float | None = None,
) -> tuple[ProtectedTailSelectionFact, ProviderPrefixCutProof]:
    """Freeze one of the closed longest-suffix tail candidates."""

    retained, earliest_id, tail_start, boundary = _retained_tail_coordinates(
        source_view=source_view,
        complete_tool_groups=complete_tool_groups,
        retained_group_count=retained_group_count,
    )
    messages = source_view.materialized_messages()
    if (source_projection is None) != (summary_prefix_message_count is None):
        raise ValueError("explicit summary boundary inputs are incomplete")
    if source_projection is not None:
        safe = dict(
            _safe_summary_boundaries(
                source_view=source_view,
                source_projection=source_projection,
                maximum_message_count=tail_start,
                deadline_monotonic=deadline_monotonic,
            )
        )
        assert summary_prefix_message_count is not None
        selected = safe.get(summary_prefix_message_count)
        if selected is None:
            raise CompactionPlanningError("requested summary boundary is not safe")
        tail_start = summary_prefix_message_count
        boundary = selected
    return _freeze_tail_and_prefix_facts(
        source_view=source_view,
        messages=messages,
        retained=retained,
        earliest_id=earliest_id,
        tail_start=tail_start,
        boundary=boundary,
    )


def _retained_tail_coordinates(
    *,
    source_view: FrozenCompactionSourceView,
    complete_tool_groups: tuple[CompleteToolGroup, ...],
    retained_group_count: int,
) -> tuple[tuple[CompleteToolGroup, ...], str | None, int, int]:
    if not 0 <= retained_group_count <= len(complete_tool_groups):
        raise ValueError("retained compaction group count is out of bounds")
    retained = (
        complete_tool_groups[-retained_group_count:] if retained_group_count else ()
    )
    messages = source_view.materialized_messages()
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
    return retained, earliest_id, tail_start, boundary


def _safe_summary_boundaries(
    *,
    source_view: FrozenCompactionSourceView,
    source_projection: FrozenModelInputSemanticProjection,
    maximum_message_count: int,
    deadline_monotonic: float | None,
) -> tuple[tuple[int, int], ...]:
    """Compute every complete boundary in one suffix-minimum traversal."""

    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise TimeoutError("compaction prefix planning deadline expired")
    messages = source_view.materialized_messages()
    if (
        source_projection.messages != messages
        or source_projection.system_prompt != source_view.materialized_system_prompt()
        or source_projection.tools
        != source_view.normal_compile_binding.tool_surface.tool_specs
        or source_projection.compile_binding_fingerprint
        != source_view.normal_compile_binding.binding_fingerprint
        or not 0 <= maximum_message_count <= len(messages)
    ):
        raise CompactionPlanningError(
            "budgeted prefix projection differs from its source view"
        )
    canonical = source_view.canonical_dispatch_read.compile_snapshot.canonical_input
    sequence_by_entry = {
        item.source_entry_id: item.source_entry_sequence
        for item in canonical.items
        if item.source_entry_id is not None and item.source_entry_sequence is not None
    }
    placements = source_projection.message_placements

    def placement_sequences(placement) -> tuple[int, ...]:
        attachment = getattr(placement, "tool_attachment_source", None)
        if attachment is not None:
            values = tuple(
                member.source.source_entry_sequence
                for member in attachment.members
            )
            if any(value is None for value in values):
                raise CompactionPlanningError(
                    "tool attachment source lacks canonical sequence"
                )
            return tuple(int(value) for value in values)
        if placement.origin_entry_id is None:
            return ()
        sequence = sequence_by_entry.get(placement.origin_entry_id)
        return () if sequence is None else (sequence,)
    minimum_message_count = 1
    predecessor = source_view.predecessor_epoch_view
    if predecessor is not None:
        minimum_message_count = len(predecessor.messages)
        if minimum_message_count < 1:
            raise CompactionPlanningError(
                "installed provider prefix has no materialized messages"
            )
        if minimum_message_count > maximum_message_count:
            raise CompactionPlanningError(
                "retained tail would truncate the installed provider prefix"
            )
    suffix_minimum: list[int | None] = [None] * (len(placements) + 1)
    for index in range(len(placements) - 1, -1, -1):
        placement = placements[index]
        sequences = placement_sequences(placement)
        sequence = None if not sequences else min(sequences)
        later = suffix_minimum[index + 1]
        suffix_minimum[index] = (
            later
            if sequence is None
            else sequence
            if later is None
            else min(sequence, later)
        )
    safe_by_count: dict[int, int] = {}
    if maximum_message_count == len(messages):
        safe_by_count[maximum_message_count] = source_view.exact_safe_canonical_head
    for count in range(minimum_message_count, maximum_message_count + 1):
        if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
            raise TimeoutError("compaction prefix planning deadline expired")
        placement = placements[count - 1]
        entry_id = placement.origin_entry_id
        current_sequences = placement_sequences(placement)
        sequence = None if not current_sequences else max(current_sequences)
        if sequence is None or messages[count - 1].tool_calls:
            continue
        if count < len(messages):
            next_message = messages[count]
            next_placement = placements[count]
            next_attachment = getattr(next_placement, "tool_attachment_source", None)
            if (
                next_message.tool_call_id is not None
                or next_placement.origin_entry_id == entry_id
                or (
                    entry_id is not None
                    and next_attachment is not None
                    and any(
                        member.source.source_entry_id == entry_id
                        for member in next_attachment.members
                    )
                )
            ):
                continue
        later_minimum = suffix_minimum[count]
        if later_minimum is not None and later_minimum <= sequence:
            continue
        safe_by_count[count] = sequence
    if not safe_by_count:
        raise NoSafeCompactionSummaryPrefix("source view has no safe summary prefix")
    return tuple(sorted(safe_by_count.items(), reverse=True))


def _freeze_tail_and_prefix_facts(
    *,
    source_view: FrozenCompactionSourceView,
    messages: tuple[LLMMessage, ...],
    retained: tuple[CompleteToolGroup, ...],
    earliest_id: str | None,
    tail_start: int,
    boundary: int,
) -> tuple[ProtectedTailSelectionFact, ProviderPrefixCutProof]:
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


def enumerate_safe_summary_prefixes(
    *,
    source_view: FrozenCompactionSourceView,
    complete_tool_groups: tuple[CompleteToolGroup, ...],
    retained_group_count: int,
    source_projection: FrozenModelInputSemanticProjection,
    deadline_monotonic: float | None = None,
) -> tuple[tuple[ProtectedTailSelectionFact, ProviderPrefixCutProof], ...]:
    """Return every complete safe prefix, longest first, without token pruning."""

    retained, earliest_id, tail_start, _boundary = _retained_tail_coordinates(
        source_view=source_view,
        complete_tool_groups=complete_tool_groups,
        retained_group_count=retained_group_count,
    )
    messages = source_view.materialized_messages()
    boundaries = _safe_summary_boundaries(
        source_view=source_view,
        source_projection=source_projection,
        maximum_message_count=tail_start,
        deadline_monotonic=deadline_monotonic,
    )
    return tuple(
        _freeze_tail_and_prefix_facts(
            source_view=source_view,
            messages=messages,
            retained=retained,
            earliest_id=earliest_id,
            tail_start=count,
            boundary=boundary,
        )
        for count, boundary in boundaries
    )


def select_recent_human_messages(
    *,
    canonical_read: FrozenCompactionCanonicalRead,
    source_through_sequence: int,
    policy: ResolvedCompactionPolicy,
    excluded_entry_id: str | None = None,
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
        and item.source_entry_id != excluded_entry_id
        and item.source_entry_sequence is not None
        and item.source_entry_sequence <= source_through_sequence
    )
    selected: list[RecentHumanMessageProof] = []
    aggregate = 0
    for item in reversed(items):
        size = sum(
            len(part.text.encode("utf-8"))
            for part in item.content
            if isinstance(part, LLMTextPart)
        )
        if aggregate + size > policy.maximum_recent_human_text_utf8_bytes:
            if not selected:
                return ()
            break
        selected.append(
            RecentHumanMessageProof(
                entry_id=item.source_entry_id,
                entry_sequence=item.source_entry_sequence,
                input_origin=item.input_origin,
                content=FrozenPromptContent(item.content),
            )
        )
        aggregate += size
        if len(selected) == policy.maximum_recent_human_messages:
            break
    return tuple(reversed(selected))


def freeze_compaction_continuation(
    *,
    source_view: FrozenCompactionSourceView,
    target_branch: CompactionTargetBranch,
    source_through_sequence: int,
) -> tuple[CompactionContinuationMode, FrozenCompactionActiveRequest | None]:
    """Classify the successor from canonical ownership, never summary prose."""

    if target_branch is CompactionTargetBranch.IDLE_BASE_ONLY:
        return CompactionContinuationMode.AWAIT_NEXT_USER, None
    if target_branch is not CompactionTargetBranch.ACTIVE_INSTALLATION:
        raise ValueError("unsupported compaction target branch")
    canonical = source_view.canonical_dispatch_read.compile_snapshot.canonical_input
    if (
        not 0
        <= source_through_sequence
        <= canonical.identity.provider_input_through_sequence
    ):
        raise ValueError("compaction continuation boundary is out of range")
    active_entry_id = canonical.identity.initial_entry_id
    matches = tuple(
        item for item in canonical.items if item.source_entry_id == active_entry_id
    )
    if len(matches) > 1:
        raise CompactionPlanningError("active request attribution is duplicated")
    if matches:
        item = matches[0]
        request_like = (
            item.item_kind is FrozenProviderInputItemKind.USER
            and item.input_origin
            in {
                CanonicalInputOriginKind.HUMAN_MESSAGE,
                CanonicalInputOriginKind.SUBAGENT_OBJECTIVE,
            }
        ) or item.item_kind in {
            FrozenProviderInputItemKind.INTER_AGENT_MESSAGE,
            FrozenProviderInputItemKind.PLAN_CONTINUATION,
            FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
        }
        if not request_like or item.source_entry_sequence is None:
            raise CompactionPlanningError("active request is not a canonical request")
        location = (
            CompactionActiveRequestLocation.SNAPSHOT_EXACT
            if item.source_entry_sequence <= source_through_sequence
            else CompactionActiveRequestLocation.CANONICAL_SUFFIX
        )
        return (
            CompactionContinuationMode.RESUME_ACTIVE_TURN,
            FrozenCompactionActiveRequest(
                entry_id=active_entry_id,
                entry_sequence=item.source_entry_sequence,
                location=location,
                item_kind=item.item_kind,
                input_origin=item.input_origin,
                content=(
                    FrozenPromptContent(item.content)
                    if location is CompactionActiveRequestLocation.SNAPSHOT_EXACT
                    else None
                ),
            ),
        )

    predecessor = source_view.snapshot_carrier
    if predecessor is None:
        raise CompactionPlanningError("active request is absent without one snapshot")
    active = predecessor.active_request
    if (
        predecessor.continuation_mode
        is not CompactionContinuationMode.RESUME_ACTIVE_TURN
        or active is None
        or active.entry_id != active_entry_id
        or active.location is not CompactionActiveRequestLocation.SNAPSHOT_EXACT
        or active.content is None
    ):
        raise CompactionPlanningError(
            "predecessor snapshot does not carry the active request"
        )
    return (
        CompactionContinuationMode.RESUME_ACTIVE_TURN,
        FrozenCompactionActiveRequest(
            entry_id=active.entry_id,
            entry_sequence=active.entry_sequence,
            location=CompactionActiveRequestLocation.SNAPSHOT_EXACT,
            item_kind=active.item_kind,
            input_origin=active.input_origin,
            content=active.content,
        ),
    )


def should_trigger_compaction(
    *,
    source_view: FrozenCompactionSourceView,
    policy: ResolvedCompactionPolicy,
    force: bool,
) -> bool:
    if force:
        return True
    budget = source_view.normal_compile_binding.effective_input_budget_tokens
    estimate = source_view.provider_wire_quote.final_wire_estimated_input_tokens
    token_trigger = estimate >= int(budget * policy.auto_trigger_ratio)
    working = source_view.physical_working_set
    headroom_trigger = crosses_compaction_resource_headroom(
        selected_item_count=working.selected_item_count,
        selected_canonical_expanded_bytes=(
            working.selected_canonical_expanded_bytes
        ),
        continuity_epoch_logical_bytes=(
            working.continuity_epoch_logical_bytes
        ),
    )
    return token_trigger or headroom_trigger


def crosses_compaction_resource_headroom(
    *,
    selected_item_count: int,
    selected_canonical_expanded_bytes: int,
    continuity_epoch_logical_bytes: int,
) -> bool:
    bounds = resolved_compaction_headroom_bounds()
    return (
        selected_item_count >= bounds.soft_canonical_item_limit
        or selected_canonical_expanded_bytes
        >= bounds.soft_canonical_expanded_byte_limit
        or continuity_epoch_logical_bytes
        >= bounds.soft_epoch_logical_byte_limit
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
        raise CompactionReclaimUnavailable(
            "compaction successor does not reclaim context"
        )
    if (
        reclaim < policy.minimum_reclaim_tokens
        and source_tokens <= hard_input_budget_tokens
    ):
        raise CompactionReclaimUnavailable(
            "compaction successor does not reclaim enough context"
        )
    if (
        enforce_soft_target
        and not force
        and successor_tokens
        > int(hard_input_budget_tokens * policy.post_compaction_target_ratio)
    ):
        raise CompactionPlanningError("compaction successor exceeds its post target")
    return reclaim


def _logical_input_bytes(
    system_prompt: str,
    messages: tuple[LLMMessage, ...],
    tools: tuple[FrozenToolSpec, ...],
) -> int:
    return provider_input_logical_bytes(
        system_prompt=system_prompt,
        tools=tools,
        messages=messages,
    )


__all__ = [
    "CompactionPlanningError",
    "CompactionReclaimUnavailable",
    "crosses_compaction_resource_headroom",
    "enumerate_complete_tool_groups",
    "enumerate_safe_summary_prefixes",
    "freeze_compaction_continuation",
    "freeze_compaction_source_view",
    "freeze_tail_and_prefix",
    "provider_dispatch_effective_history_has_image",
    "select_recent_human_messages",
    "should_trigger_compaction",
]
