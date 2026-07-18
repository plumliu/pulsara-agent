"""Deterministic tool-observation projection planning."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING

from pulsara_agent.event import ContextProjectionRewritePageEvent, EventContext
from pulsara_agent.primitives.context import (
    ContextInputFailureReasonCode,
    ResolvedToolResultRenderPolicyFact,
    TranscriptCompileInput,
    context_fingerprint,
)
from pulsara_agent.primitives.long_horizon import (
    ContextWindowFact,
    ContextWindowProjectionState,
    LongHorizonContextAllocationPolicyFact,
    ObservationRollupFact,
    ProjectionTargetUnreachableAuditFact,
    ProjectionRewriteReason,
    TOOL_OBSERVATION_REPRESENTATION_RANK,
    ToolObservationProjectionFact,
    ToolObservationProjectionRewriteEntryFact,
    ToolObservationProtectionFact,
    ToolObservationRepresentation,
)
from pulsara_agent.primitives.model_call import (
    ResolvedModelContextBudgetFact,
    TokenEstimatorFact,
)
from pulsara_agent.primitives.tool_result import (
    ToolResultBodyPolicy,
    ToolResultEnvelopePolicy,
    ToolResultRenderDecisionFact,
    ToolResultRenderUnit,
)
from pulsara_agent.runtime.context_input.render import (
    PreparedToolResultRenderOutput,
    RenderedToolResultFragment,
    render_tool_result_projection_variant,
)
from pulsara_agent.runtime.long_horizon.rollup import (
    ObservationRollupRendererRegistry,
    PreparedObservationRollupArtifact,
    prepare_observation_rollup_artifact,
)
from pulsara_agent.runtime.long_horizon.projection_reducer import (
    advance_projection_generation,
    build_projection_state,
)

if TYPE_CHECKING:
    from pulsara_agent.llm.estimator import TokenEstimator
    from pulsara_agent.runtime.context_input.event_slice import ContextEventSlice


@dataclass(frozen=True, slots=True)
class ContextProjectionRewritePlan:
    rewrite_id: str
    source_through_sequence: int
    events: tuple[ContextProjectionRewritePageEvent, ...]
    final_state: ContextWindowProjectionState
    plan_fingerprint: str
    prepared_rollup_artifacts: tuple[PreparedObservationRollupArtifact, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectionTargetUnreachable:
    target_projected_tokens: int
    minimum_projected_tokens: int
    source_projection_generation: int
    minimum_plan: ContextProjectionRewritePlan | None
    reason_code: str = "projection_target_unreachable"


ProjectionPlanningResult = ContextProjectionRewritePlan | ProjectionTargetUnreachable


class LongHorizonPreparationBoundExceeded(RuntimeError):
    """One independent preparation counter reached its run-frozen bound."""

    def __init__(self, reason_code: ContextInputFailureReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


def advance_compile_attempt_index(
    current: int,
    *,
    policy: LongHorizonContextAllocationPolicyFact,
) -> int:
    if current < 0:
        raise ValueError("compile attempt index cannot be negative")
    if current >= policy.max_compile_attempts_per_model_call:
        raise LongHorizonPreparationBoundExceeded(
            ContextInputFailureReasonCode.CONTEXT_COMPILE_ATTEMPTS_EXHAUSTED
        )
    return current + 1


def advance_safe_point_revision(
    current: int,
    *,
    policy: LongHorizonContextAllocationPolicyFact,
) -> int:
    if current < 0:
        raise ValueError("safe-point revision cannot be negative")
    if current >= policy.max_safe_point_revisions:
        raise LongHorizonPreparationBoundExceeded(
            ContextInputFailureReasonCode.LONG_HORIZON_PREPARATION_CYCLE_EXCEEDED
        )
    return current + 1


@dataclass(frozen=True, slots=True)
class CurrentRunProjectionPlanningInput:
    run_id: str
    run_start_sequence: int
    window: ContextWindowFact
    current_projection: ContextWindowProjectionState
    canonical_slice: ContextEventSlice
    transcript: TranscriptCompileInput
    tool_result_units: tuple[ToolResultRenderUnit, ...]
    protection_facts: tuple[ToolObservationProtectionFact, ...]
    context_budget: ResolvedModelContextBudgetFact
    allocation_policy: LongHorizonContextAllocationPolicyFact
    estimator: TokenEstimatorFact
    source_through_sequence: int

    def __post_init__(self) -> None:
        if self.run_start_sequence < 1:
            raise ValueError("current-run boundary requires RunStart sequence")
        if self.source_through_sequence != self.canonical_slice.through_sequence:
            raise ValueError("current-run planning high-water differs from event slice")
        if self.source_through_sequence != self.transcript.through_sequence:
            raise ValueError("current-run planning high-water differs from transcript")
        if self.current_projection.window_id != self.window.window_id:
            raise ValueError("current-run planning projection/window mismatch")
        if self.context_budget.input_budget_tokens != self.window.input_budget_tokens:
            raise ValueError("current-run planning budget/window mismatch")
        if (
            self.estimator.estimator_fingerprint
            != self.window.token_estimator_fingerprint
        ):
            raise ValueError("current-run planning estimator/window mismatch")
        unit_ids = tuple(unit.unit_id for unit in self.tool_result_units)
        protection_ids = tuple(fact.unit_id for fact in self.protection_facts)
        if unit_ids != protection_ids or len(unit_ids) != len(set(unit_ids)):
            raise ValueError("current-run planning protection facts are incomplete")


def prepare_current_run_projection_planning_input(
    *,
    run_id: str,
    run_start_sequence: int,
    window: ContextWindowFact,
    current_projection: ContextWindowProjectionState,
    canonical_slice: ContextEventSlice,
    transcript: TranscriptCompileInput,
    tool_result_units: tuple[ToolResultRenderUnit, ...],
    context_budget: ResolvedModelContextBudgetFact,
    allocation_policy: LongHorizonContextAllocationPolicyFact,
    estimator: TokenEstimatorFact,
    pending_interaction: bool,
    tool_call_in_flight: bool,
    artifact_write_pending_unit_ids: frozenset[str] = frozenset(),
    explicit_user_evidence_unit_ids: frozenset[str] = frozenset(),
    unconsumed_subagent_result_unit_ids: frozenset[str] = frozenset(),
) -> CurrentRunProjectionPlanningInput:
    """Derive current-run boundaries and protection facts from one frozen slice."""

    messages = {message.message_id: message for message in transcript.messages}
    pairs = {pair.tool_call_id: pair for pair in transcript.tool_pairs}
    current_unit_ids: set[str] = set()
    segment_by_unit: dict[str, str] = {}
    for unit in tool_result_units:
        pair = pairs.get(unit.tool_call_id)
        if (
            pair is None
            or pair.call_message_id != unit.call_message_id
            or pair.result_message_id != unit.result_message_id
        ):
            raise ValueError("current-run planning unit/pair identity mismatch")
        call_message = messages.get(pair.call_message_id)
        result_message = messages.get(pair.result_message_id)
        if call_message is None or result_message is None:
            raise ValueError("current-run planning pair message is missing")
        if unit.source_sequence_end > canonical_slice.through_sequence:
            raise ValueError("current-run result exceeds canonical high-water")
        segment_by_unit[unit.unit_id] = result_message.segment
        is_current = (
            pair.call_sequence >= run_start_sequence
            and pair.result_sequence >= run_start_sequence
        )
        if result_message.segment == "current_run_tail" and not is_current:
            raise ValueError("current-run transcript segment crosses run boundary")
        if is_current:
            if result_message.segment not in {"current_user", "current_run_tail"}:
                raise ValueError("current-run result is outside the active tail")
            current_unit_ids.add(unit.unit_id)

    recent_ids = frozenset(
        unit.unit_id
        for unit in sorted(
            (unit for unit in tool_result_units if unit.unit_id in current_unit_ids),
            key=lambda unit: (unit.source_sequence_end, unit.unit_id),
        )[-allocation_policy.current_run_recent_unit_count :]
    )
    facts: list[ToolObservationProtectionFact] = []
    for unit in tool_result_units:
        classes: set[str] = set()
        if segment_by_unit[unit.unit_id] == "current_user":
            classes.add("current_user_adjacent")
        if unit.unit_id in recent_ids:
            classes.add("current_run_recent")
        if (
            unit.unit_id in current_unit_ids
            and unit.unit_id in recent_ids
            and unit.result_state.value in {"error", "interrupted", "denied"}
        ):
            classes.add("error_recovery")
        if unit.unit_id in current_unit_ids and pending_interaction:
            classes.add("pending_interaction")
        if unit.unit_id in current_unit_ids and tool_call_in_flight:
            classes.add("tool_call_in_flight")
        if unit.unit_id in artifact_write_pending_unit_ids:
            classes.add("artifact_write_pending")
        if unit.unit_id in explicit_user_evidence_unit_ids:
            classes.add("explicit_user_requested_evidence")
        if unit.unit_id in unconsumed_subagent_result_unit_ids:
            classes.add("unconsumed_subagent_result")
        ordered_classes = tuple(sorted(classes))
        minimum = _minimum_representation_for_protection(ordered_classes)
        payload = {
            "unit_id": unit.unit_id,
            "classes": ordered_classes,
            "minimum_representation": minimum,
        }
        facts.append(
            ToolObservationProtectionFact(
                **payload,
                protection_fingerprint=context_fingerprint(
                    "tool-observation-protection:v1", payload
                ),
            )
        )
    return CurrentRunProjectionPlanningInput(
        run_id=run_id,
        run_start_sequence=run_start_sequence,
        window=window,
        current_projection=current_projection,
        canonical_slice=canonical_slice,
        transcript=transcript,
        tool_result_units=tool_result_units,
        protection_facts=tuple(facts),
        context_budget=context_budget,
        allocation_policy=allocation_policy,
        estimator=estimator,
        source_through_sequence=canonical_slice.through_sequence,
    )


def _minimum_representation_for_protection(
    classes: tuple[str, ...],
) -> ToolObservationRepresentation:
    protected = frozenset(classes)
    if protected & {
        "pending_interaction",
        "artifact_write_pending",
        "tool_call_in_flight",
    }:
        return ToolObservationRepresentation.FULL
    if protected & {
        "current_user_adjacent",
        "error_recovery",
        "explicit_user_requested_evidence",
        "unconsumed_subagent_result",
    }:
        return ToolObservationRepresentation.ESSENTIAL
    if "current_run_recent" in protected:
        return ToolObservationRepresentation.PREVIEW
    return ToolObservationRepresentation.PAIR_STUB


def projection_target_unreachable_audit(
    outcome: ProjectionTargetUnreachable,
) -> ProjectionTargetUnreachableAuditFact:
    payload = {
        "target_projected_tokens": outcome.target_projected_tokens,
        "minimum_projected_tokens": outcome.minimum_projected_tokens,
        "source_projection_generation": outcome.source_projection_generation,
        "reason_code": outcome.reason_code,
    }
    return ProjectionTargetUnreachableAuditFact(
        **payload,
        audit_fingerprint=context_fingerprint(
            "projection-target-unreachable-audit:v1",
            {
                "schema_version": "projection_target_unreachable_audit.v1",
                **payload,
            },
        ),
    )


def plan_new_result_ingest(
    *,
    event_context: EventContext,
    window: ContextWindowFact,
    current_state: ContextWindowProjectionState,
    units: tuple[ToolResultRenderUnit, ...],
    rendered: PreparedToolResultRenderOutput,
    token_estimator: TokenEstimator,
    policy: LongHorizonContextAllocationPolicyFact,
    protection_facts: tuple[ToolObservationProtectionFact, ...],
    source_through_sequence: int,
) -> ContextProjectionRewritePlan | None:
    """Plan one generation that admits every newly terminal result.

    L0B does not degrade representation.  It records the exact Stage-3 render
    fragment and its highest safe representation so later stages can rewrite
    from one durable generation rather than infer from payload text.
    """

    max_entries_per_page = policy.max_rewrite_entries_per_page
    if current_state.window_id != window.window_id:
        raise ValueError("projection planner window/state mismatch")
    if source_through_sequence < current_state.through_sequence:
        raise ValueError("projection source high-water moved backwards")
    if not (
        len(units)
        == len(rendered.fragments)
        == len(rendered.canonical_decisions)
    ):
        raise ValueError("projection planner render tuple cardinality mismatch")
    existing = {item.unit_id: item for item in current_state.unit_projections}
    protection_by_id = _protection_facts_by_unit(
        units=units,
        protection_facts=protection_facts,
    )
    to_generation = current_state.projection_generation + 1
    entries: list[ToolObservationProjectionRewriteEntryFact] = []
    for unit, fragment, decision in zip(
        units,
        rendered.fragments,
        rendered.canonical_decisions,
        strict=True,
    ):
        _validate_render_identity(unit, fragment.unit_id, decision)
        previous = existing.get(unit.unit_id)
        if previous is not None:
            if (
                previous.tool_call_id != unit.tool_call_id
                or previous.tool_result_event_id != unit.source_event_ids[-1]
            ):
                raise ValueError("existing projection unit differs from durable source")
            desired_protection = protection_by_id[unit.unit_id]
            if previous.protected_reason_codes != desired_protection.classes:
                updated = _projection_with_protection(
                    previous,
                    projection_generation=to_generation,
                    protection=desired_protection,
                )
                entries.append(
                    ToolObservationProjectionRewriteEntryFact(
                        unit_id=unit.unit_id,
                        from_representation=previous.representation,
                        to_projection=updated,
                    )
                )
            continue
        if unit.source_sequence_end > source_through_sequence:
            raise ValueError("projection unit exceeds source high-water")
        projection = _initial_projection(
            window=window,
            projection_generation=to_generation,
            unit=unit,
            fragment_text=fragment.text,
            fragment_fingerprint=fragment.rendered_text_fingerprint,
            protection=protection_by_id[unit.unit_id],
            decision=decision,
            token_estimator=token_estimator,
        )
        entries.append(
            ToolObservationProjectionRewriteEntryFact(
                unit_id=unit.unit_id,
                from_representation=None,
                to_projection=projection,
            )
        )
    if not entries:
        return None

    advanced = {
        item.unit_id: advance_projection_generation(
            item,
            projection_generation=to_generation,
        )
        for item in current_state.unit_projections
    }
    advanced.update({entry.unit_id: entry.to_projection for entry in entries})
    final_units = tuple(
        sorted(
            advanced.values(),
            key=lambda item: (item.tool_result_sequence, item.unit_id),
        )
    )
    final_state = build_projection_state(
        window=window,
        projection_generation=to_generation,
        through_sequence=source_through_sequence,
        unit_projections=final_units,
        rollups=current_state.rollups,
    )
    plan_payload = {
        "window_id": window.window_id,
        "from_projection_generation": current_state.projection_generation,
        "to_projection_generation": to_generation,
        "source_through_sequence": source_through_sequence,
        "entries": tuple(entries),
        "rollups": current_state.rollups,
        "reason_code": ProjectionRewriteReason.NEW_RESULT_INGESTED,
        "final_state_fingerprint": final_state.state_semantic_fingerprint,
    }
    plan_fingerprint = context_fingerprint(
        "context-projection-rewrite-plan:v1", plan_payload
    )
    rewrite_id = f"context_projection_rewrite:{plan_fingerprint.removeprefix('sha256:')}"
    page_count = ceil(len(entries) / max_entries_per_page)
    events = tuple(
        ContextProjectionRewritePageEvent(
            id=f"{rewrite_id}:page:{page_index}",
            **event_context.event_fields(),
            rewrite_id=rewrite_id,
            window_id=window.window_id,
            from_projection_generation=current_state.projection_generation,
            to_projection_generation=to_generation,
            source_through_sequence=source_through_sequence,
            page_index=page_index,
            page_count=page_count,
            entries=tuple(
                entries[
                    page_index
                    * max_entries_per_page : (page_index + 1)
                    * max_entries_per_page
                ]
            ),
            rollups=current_state.rollups if page_index == 0 else (),
            plan_fingerprint=plan_fingerprint,
            final_state_fingerprint=final_state.state_semantic_fingerprint,
            reason_code=ProjectionRewriteReason.NEW_RESULT_INGESTED,
        )
        for page_index in range(page_count)
    )
    return ContextProjectionRewritePlan(
        rewrite_id=rewrite_id,
        source_through_sequence=source_through_sequence,
        events=events,
        final_state=final_state,
        plan_fingerprint=plan_fingerprint,
    )


def _initial_projection(
    *,
    window: ContextWindowFact,
    projection_generation: int,
    unit: ToolResultRenderUnit,
    fragment_text: str,
    fragment_fingerprint: str,
    protection: ToolObservationProtectionFact,
    decision: ToolResultRenderDecisionFact,
    token_estimator: TokenEstimator,
) -> ToolObservationProjectionFact:
    representation = _representation(unit=unit, decision=decision)
    payload = {
        "schema_version": "tool_observation_projection.v1",
        "window_id": window.window_id,
        "projection_generation": projection_generation,
        "unit_id": unit.unit_id,
        "tool_call_id": unit.tool_call_id,
        "tool_result_event_id": unit.source_event_ids[-1],
        "tool_result_sequence": unit.source_sequence_end,
        "tool_name": unit.model_tool_name,
        "representation": representation,
        "representation_rank": TOOL_OBSERVATION_REPRESENTATION_RANK[representation],
        "rendered_fragment_artifact_id": None,
        "rendered_fragment_fingerprint": fragment_fingerprint,
        "estimated_tokens": token_estimator.estimate_text(fragment_text),
        "primary_artifact_id": decision.primary_artifact_id,
        "essential_envelope_fingerprint": context_fingerprint(
            "tool-observation-essential-envelope:v1",
            unit.essential,
        ),
        "observation_timing_fingerprint": context_fingerprint(
            "tool-observation-timing:v1",
            unit.observation_timing,
        ),
        "source_rollup_id": None,
        "protected_reason_codes": protection.classes,
        "decision_reason_code": ProjectionRewriteReason.NEW_RESULT_INGESTED,
    }
    return ToolObservationProjectionFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "tool-observation-projection:v1", payload
        ),
    )


def _protection_facts_by_unit(
    *,
    units: tuple[ToolResultRenderUnit, ...],
    protection_facts: tuple[ToolObservationProtectionFact, ...],
) -> dict[str, ToolObservationProtectionFact]:
    unit_ids = tuple(unit.unit_id for unit in units)
    protection_ids = tuple(fact.unit_id for fact in protection_facts)
    if unit_ids != protection_ids or len(unit_ids) != len(set(unit_ids)):
        raise ValueError("projection protection facts must match ordered units")
    return {fact.unit_id: fact for fact in protection_facts}


def _projection_with_protection(
    projection: ToolObservationProjectionFact,
    *,
    projection_generation: int,
    protection: ToolObservationProtectionFact,
) -> ToolObservationProjectionFact:
    payload = projection.model_dump(
        mode="python",
        exclude={"projection_generation", "protected_reason_codes", "semantic_fingerprint"},
    )
    payload.update(
        {
            "projection_generation": projection_generation,
            "protected_reason_codes": protection.classes,
            "decision_reason_code": ProjectionRewriteReason.NEW_RESULT_INGESTED,
        }
    )
    return ToolObservationProjectionFact(
        **payload,
        semantic_fingerprint=context_fingerprint(
            "tool-observation-projection:v1", payload
        ),
    )


def _representation(
    *,
    unit: ToolResultRenderUnit,
    decision: ToolResultRenderDecisionFact,
) -> ToolObservationRepresentation:
    if (
        decision.body_policy is ToolResultBodyPolicy.FULL_VISIBLE
        and decision.envelope_policy is ToolResultEnvelopePolicy.FULL
    ):
        return ToolObservationRepresentation.FULL
    if decision.visible_body_chars > 0:
        return ToolObservationRepresentation.PREVIEW
    if decision.primary_artifact_id is not None:
        return ToolObservationRepresentation.ARTIFACT_LOCATOR
    if unit.essential is not None:
        return ToolObservationRepresentation.ESSENTIAL
    return ToolObservationRepresentation.PAIR_STUB


@dataclass(frozen=True, slots=True)
class _Variant:
    representation: ToolObservationRepresentation
    fragment: RenderedToolResultFragment
    decision: ToolResultRenderDecisionFact
    estimated_tokens: int
    source_rollup_id: str | None


def plan_deterministic_projection_rewrite(
    *,
    event_context: EventContext,
    window: ContextWindowFact,
    current_state: ContextWindowProjectionState,
    units: tuple[ToolResultRenderUnit, ...],
    base_rendered: PreparedToolResultRenderOutput,
    render_policy: ResolvedToolResultRenderPolicyFact,
    transcript,
    token_estimator: TokenEstimator,
    policy: LongHorizonContextAllocationPolicyFact,
    protection_facts: tuple[ToolObservationProtectionFact, ...],
    target_projected_tokens: int,
    source_through_sequence: int,
    rollup_registry: ObservationRollupRendererRegistry,
    runtime_observation_carrier_available: bool,
) -> ProjectionPlanningResult | None:
    """Produce one deterministic minimum-or-target projection generation."""

    if target_projected_tokens < 0:
        raise ValueError("projection target must be non-negative")
    if current_state.window_id != window.window_id:
        raise ValueError("projection rewrite window/state mismatch")
    if source_through_sequence < current_state.through_sequence:
        raise ValueError("projection rewrite source high-water moved backwards")
    if not (
        len(units)
        == len(base_rendered.fragments)
        == len(base_rendered.canonical_decisions)
    ):
        raise ValueError("projection rewrite render cardinality mismatch")
    unit_by_id = {unit.unit_id: unit for unit in units}
    protection_by_id = _protection_facts_by_unit(
        units=units,
        protection_facts=protection_facts,
    )
    fragment_by_id = {item.unit_id: item for item in base_rendered.fragments}
    decision_by_id = {
        item.unit_id: item for item in base_rendered.canonical_decisions
    }
    if len(unit_by_id) != len(units):
        raise ValueError("projection rewrite contains duplicate source units")
    current_by_id = {item.unit_id: item for item in current_state.unit_projections}
    missing = tuple(unit_id for unit_id in unit_by_id if unit_id not in current_by_id)
    if missing:
        raise ValueError("projection rewrite requires prior new-result ingestion")
    if any(
        current_by_id[unit_id].protected_reason_codes
        != protection_by_id[unit_id].classes
        for unit_id in unit_by_id
    ):
        raise ValueError("projection rewrite protection facts differ from state")
    selected: dict[str, _Variant] = {}
    for unit_id, projection in current_by_id.items():
        unit = unit_by_id.get(unit_id)
        if unit is None:
            continue
        base_fragment = fragment_by_id[unit_id]
        if (
            projection.source_rollup_id is None
            and projection.rendered_fragment_fingerprint
            == base_fragment.rendered_text_fingerprint
            and projection.estimated_tokens
            == token_estimator.estimate_text(base_fragment.text)
        ):
            selected[unit_id] = _Variant(
                representation=projection.representation,
                fragment=base_fragment,
                decision=decision_by_id[unit_id],
                estimated_tokens=projection.estimated_tokens,
                source_rollup_id=None,
            )
        else:
            selected[unit_id] = _render_variant(
                unit=unit,
                base_fragment=base_fragment,
                base_decision=decision_by_id[unit_id],
                representation=projection.representation,
                source_rollup_id=projection.source_rollup_id,
                policy=render_policy,
                token_estimator=token_estimator,
            )

    active_rollups = list(current_state.rollups)
    prepared_rollups: list[PreparedObservationRollupArtifact] = []
    changed_reason: dict[str, ProjectionRewriteReason] = {}
    total = _selected_projection_tokens(selected, active_rollups)

    if total > target_projected_tokens and runtime_observation_carrier_available:
        candidates = _rollup_candidates(
            window=window,
            current_state=current_state,
            units=units,
            selected=selected,
            fragments=fragment_by_id,
            decisions=decision_by_id,
            transcript=transcript,
            policy=policy,
            render_policy=render_policy,
            token_estimator=token_estimator,
            registry=rollup_registry,
        )
        used_members: set[str] = {
            member.unit_id
            for rollup in active_rollups
            for member in rollup.member_facts
        }
        for savings, prepared, member_variants in candidates:
            if total <= target_projected_tokens:
                break
            if savings <= 0 or any(
                unit_id in used_members for unit_id in member_variants
            ):
                continue
            active_rollups.append(prepared.fact)
            prepared_rollups.append(prepared)
            for unit_id, variant in member_variants.items():
                selected[unit_id] = variant
                changed_reason[unit_id] = ProjectionRewriteReason.ROLLUP_CREATED
                used_members.add(unit_id)
            total -= savings

    degradation_ladders: dict[str, tuple[_Variant, ...]] = {}
    degradation_heap: list[tuple[int, int, int, str, int]] = []
    for unit_id, current_variant in (
        selected.items() if total > target_projected_tokens else ()
    ):
        unit = unit_by_id[unit_id]
        projection = current_by_id[unit_id]
        ladder: list[_Variant] = []
        cursor = current_variant
        while True:
            next_variant = _next_projection_variant(
                unit=unit,
                current=cursor,
                projection=projection,
                protection=protection_by_id[unit_id],
                base_fragment=fragment_by_id[unit_id],
                base_decision=decision_by_id[unit_id],
                policy=render_policy,
                token_estimator=token_estimator,
            )
            if next_variant is None:
                break
            ladder.append(next_variant)
            cursor = next_variant
        if not ladder:
            continue
        degradation_ladders[unit_id] = tuple(ladder)
        savings = current_variant.estimated_tokens - ladder[0].estimated_tokens
        if savings > 0:
            heapq.heappush(
                degradation_heap,
                (
                    _protection_rank(projection),
                    unit.source_sequence_end,
                    -savings,
                    unit_id,
                    0,
                ),
            )

    while total > target_projected_tokens and degradation_heap:
        _rank, _sequence, _negative_savings, unit_id, ladder_index = (
            heapq.heappop(degradation_heap)
        )
        chosen = degradation_ladders[unit_id][ladder_index]
        savings = selected[unit_id].estimated_tokens - chosen.estimated_tokens
        if savings <= 0:
            continue
        total -= savings
        selected[unit_id] = chosen
        changed_reason.setdefault(
            unit_id,
            ProjectionRewriteReason.SOFT_TARGET_EXCEEDED,
        )
        next_index = ladder_index + 1
        ladder = degradation_ladders[unit_id]
        if next_index < len(ladder):
            next_variant = ladder[next_index]
            next_savings = chosen.estimated_tokens - next_variant.estimated_tokens
            if next_savings > 0:
                unit = unit_by_id[unit_id]
                heapq.heappush(
                    degradation_heap,
                    (
                        _protection_rank(current_by_id[unit_id]),
                        unit.source_sequence_end,
                        -next_savings,
                        unit_id,
                        next_index,
                    ),
                )

    changed_ids = tuple(
        unit_id
        for unit_id, variant in selected.items()
        if (
            variant.representation != current_by_id[unit_id].representation
            or variant.source_rollup_id != current_by_id[unit_id].source_rollup_id
            or variant.fragment.rendered_text_fingerprint
            != current_by_id[unit_id].rendered_fragment_fingerprint
        )
    )
    plan = (
        _build_projection_rewrite_plan(
            event_context=event_context,
            window=window,
            current_state=current_state,
            units=unit_by_id,
            selected=selected,
            changed_ids=changed_ids,
            changed_reason=changed_reason,
            rollups=tuple(active_rollups),
            prepared_rollups=tuple(prepared_rollups),
            source_through_sequence=source_through_sequence,
            max_entries_per_page=policy.max_rewrite_entries_per_page,
        )
        if changed_ids
        else None
    )
    if total > target_projected_tokens:
        return ProjectionTargetUnreachable(
            target_projected_tokens=target_projected_tokens,
            minimum_projected_tokens=total,
            source_projection_generation=current_state.projection_generation,
            minimum_plan=plan,
        )
    return plan


def _validate_render_identity(
    unit: ToolResultRenderUnit,
    fragment_unit_id: str,
    decision: ToolResultRenderDecisionFact,
) -> None:
    if (
        fragment_unit_id != unit.unit_id
        or decision.unit_id != unit.unit_id
        or decision.tool_call_id != unit.tool_call_id
        or decision.render_source_fingerprint != unit.unit_fingerprint
    ):
        raise ValueError("projection planner unit/render identity mismatch")


def _render_variant(
    *,
    unit: ToolResultRenderUnit,
    base_fragment: RenderedToolResultFragment,
    base_decision: ToolResultRenderDecisionFact,
    representation: ToolObservationRepresentation,
    source_rollup_id: str | None,
    policy: ResolvedToolResultRenderPolicyFact,
    token_estimator: TokenEstimator,
) -> _Variant:
    fragment, decision = render_tool_result_projection_variant(
        unit=unit,
        base_fragment=base_fragment,
        base_decision=base_decision,
        representation=representation,
        source_rollup_id=source_rollup_id,
        policy=policy,
    )
    return _Variant(
        representation=representation,
        fragment=fragment,
        decision=decision,
        estimated_tokens=token_estimator.estimate_text(fragment.text),
        source_rollup_id=source_rollup_id,
    )


def _selected_projection_tokens(
    selected: dict[str, _Variant],
    rollups: list[ObservationRollupFact],
) -> int:
    return sum(item.estimated_tokens for item in selected.values()) + sum(
        item.estimated_tokens for item in rollups
    )


def _rollup_candidates(
    *,
    window: ContextWindowFact,
    current_state: ContextWindowProjectionState,
    units: tuple[ToolResultRenderUnit, ...],
    selected: dict[str, _Variant],
    fragments: dict[str, RenderedToolResultFragment],
    decisions: dict[str, ToolResultRenderDecisionFact],
    transcript,
    policy: LongHorizonContextAllocationPolicyFact,
    render_policy: ResolvedToolResultRenderPolicyFact,
    token_estimator: TokenEstimator,
    registry: ObservationRollupRendererRegistry,
) -> list[
    tuple[int, PreparedObservationRollupArtifact, dict[str, _Variant]]
]:
    projections = {item.unit_id: item for item in current_state.unit_projections}
    groups: dict[tuple[str, ...], list[ToolResultRenderUnit]] = {}
    for unit in units:
        semantics = unit.rollup_semantics
        projection = projections.get(unit.unit_id)
        if semantics is None or projection is None:
            continue
        if unit.result_state.value != "success" or projection.protected_reason_codes:
            continue
        if projection.source_rollup_id is not None:
            continue
        if projection.representation_rank <= TOOL_OBSERVATION_REPRESENTATION_RANK[
            ToolObservationRepresentation.ROLLUP_MEMBER
        ]:
            # A rollup may only narrow an observation. Older generations can
            # already have reduced a member to a pair stub before its family
            # becomes large enough to aggregate.
            continue
        key = (
            semantics.rollup_kind,
            semantics.family_key,
            semantics.renderer_id,
            semantics.renderer_version,
            semantics.renderer_contract_fingerprint,
        )
        groups.setdefault(key, []).append(unit)

    candidates: list[
        tuple[int, PreparedObservationRollupArtifact, dict[str, _Variant]]
    ] = []
    for _family_key, family_units in sorted(groups.items()):
        ordered = tuple(
            sorted(
                family_units,
                key=lambda unit: (unit.source_sequence_end, unit.unit_id),
            )
        )
        for offset in range(0, len(ordered), policy.max_rollup_members):
            chunk = ordered[offset : offset + policy.max_rollup_members]
            if len(chunk) < 2:
                continue
            try:
                prepared = prepare_observation_rollup_artifact(
                    window_id=window.window_id,
                    member_units=chunk,
                    transcript=transcript,
                    policy=policy,
                    token_estimator=token_estimator,
                    registry=registry,
                )
                member_variants = {
                    unit.unit_id: _render_variant(
                        unit=unit,
                        base_fragment=fragments[unit.unit_id],
                        base_decision=decisions[unit.unit_id],
                        representation=ToolObservationRepresentation.ROLLUP_MEMBER,
                        source_rollup_id=prepared.fact.rollup_id,
                        policy=render_policy,
                        token_estimator=token_estimator,
                    )
                    for unit in chunk
                }
            except ValueError:
                # An incomplete final pair group or a non-reducing member
                # simply makes this deterministic family ineligible.
                continue
            before = sum(selected[unit.unit_id].estimated_tokens for unit in chunk)
            after = prepared.fact.estimated_tokens + sum(
                item.estimated_tokens for item in member_variants.values()
            )
            savings = before - after
            candidates.append((savings, prepared, member_variants))
    return sorted(
        candidates,
        key=lambda item: (
            -item[0],
            item[1].fact.member_facts[0].result_sequence,
            item[1].fact.rollup_id,
        ),
    )


def _next_projection_variant(
    *,
    unit: ToolResultRenderUnit,
    current: _Variant,
    projection: ToolObservationProjectionFact,
    protection: ToolObservationProtectionFact,
    base_fragment: RenderedToolResultFragment,
    base_decision: ToolResultRenderDecisionFact,
    policy: ResolvedToolResultRenderPolicyFact,
    token_estimator: TokenEstimator,
) -> _Variant | None:
    minimum_rank = _minimum_representation_rank(
        projection,
        protection=protection,
    )
    current_rank = TOOL_OBSERVATION_REPRESENTATION_RANK[current.representation]
    ladder = tuple(ToolObservationRepresentation)
    for representation in ladder:
        rank = TOOL_OBSERVATION_REPRESENTATION_RANK[representation]
        if rank >= current_rank or rank < minimum_rank:
            continue
        if representation is ToolObservationRepresentation.ESSENTIAL and (
            unit.essential is None
        ):
            continue
        if representation is ToolObservationRepresentation.ARTIFACT_LOCATOR and (
            not _has_primary_text_artifact(unit)
        ):
            continue
        if representation is ToolObservationRepresentation.ROLLUP_MEMBER:
            if current.source_rollup_id is None:
                continue
        if (
            current.source_rollup_id is not None
            and representation is ToolObservationRepresentation.PAIR_STUB
        ):
            # Every active rollup member retains a member stub or higher.
            continue
        try:
            candidate = _render_variant(
                unit=unit,
                base_fragment=base_fragment,
                base_decision=base_decision,
                representation=representation,
                source_rollup_id=(
                    current.source_rollup_id
                    if representation is ToolObservationRepresentation.ROLLUP_MEMBER
                    else None
                ),
                policy=policy,
                token_estimator=token_estimator,
            )
        except ValueError:
            continue
        return candidate
    return None


def _minimum_representation_rank(
    projection: ToolObservationProjectionFact,
    *,
    protection: ToolObservationProtectionFact,
) -> int:
    current_rank = projection.representation_rank
    required_rank = TOOL_OBSERVATION_REPRESENTATION_RANK[
        protection.minimum_representation
    ]
    if projection.source_rollup_id is not None:
        required_rank = max(
            required_rank,
            TOOL_OBSERVATION_REPRESENTATION_RANK[
                ToolObservationRepresentation.ROLLUP_MEMBER
            ],
        )
    return min(current_rank, required_rank)


def _protection_rank(projection: ToolObservationProjectionFact) -> int:
    reasons = frozenset(projection.protected_reason_codes)
    if not reasons:
        return 0
    if reasons == {"current_run_recent"}:
        return 1
    if "error_recovery" in reasons or "unconsumed_subagent_result" in reasons:
        return 2
    return 3


def _has_primary_text_artifact(unit: ToolResultRenderUnit) -> bool:
    return any(
        item.role not in {"diagnostics", "metadata"}
        and (
            item.media_type.startswith("text/")
            or "json" in item.media_type
            or "xml" in item.media_type
            or "yaml" in item.media_type
        )
        for item in unit.artifacts
    )


def _build_projection_rewrite_plan(
    *,
    event_context: EventContext,
    window: ContextWindowFact,
    current_state: ContextWindowProjectionState,
    units: dict[str, ToolResultRenderUnit],
    selected: dict[str, _Variant],
    changed_ids: tuple[str, ...],
    changed_reason: dict[str, ProjectionRewriteReason],
    rollups: tuple[ObservationRollupFact, ...],
    prepared_rollups: tuple[PreparedObservationRollupArtifact, ...],
    source_through_sequence: int,
    max_entries_per_page: int,
) -> ContextProjectionRewritePlan:
    to_generation = current_state.projection_generation + 1
    current_by_id = {item.unit_id: item for item in current_state.unit_projections}
    entries: list[ToolObservationProjectionRewriteEntryFact] = []
    for unit_id in sorted(
        changed_ids,
        key=lambda value: (units[value].source_sequence_end, value),
    ):
        old = current_by_id[unit_id]
        variant = selected[unit_id]
        unit = units[unit_id]
        payload = {
            "schema_version": "tool_observation_projection.v1",
            "window_id": window.window_id,
            "projection_generation": to_generation,
            "unit_id": unit_id,
            "tool_call_id": unit.tool_call_id,
            "tool_result_event_id": unit.source_event_ids[-1],
            "tool_result_sequence": unit.source_sequence_end,
            "tool_name": unit.model_tool_name,
            "representation": variant.representation,
            "representation_rank": TOOL_OBSERVATION_REPRESENTATION_RANK[
                variant.representation
            ],
            "rendered_fragment_artifact_id": None,
            "rendered_fragment_fingerprint": (
                variant.fragment.rendered_text_fingerprint
            ),
            "estimated_tokens": variant.estimated_tokens,
            "primary_artifact_id": variant.decision.primary_artifact_id,
            "essential_envelope_fingerprint": old.essential_envelope_fingerprint,
            "observation_timing_fingerprint": old.observation_timing_fingerprint,
            "source_rollup_id": variant.source_rollup_id,
            "protected_reason_codes": old.protected_reason_codes,
            "decision_reason_code": changed_reason.get(
                unit_id, ProjectionRewriteReason.SOFT_TARGET_EXCEEDED
            ),
        }
        projection = ToolObservationProjectionFact(
            **payload,
            semantic_fingerprint=context_fingerprint(
                "tool-observation-projection:v1", payload
            ),
        )
        entries.append(
            ToolObservationProjectionRewriteEntryFact(
                unit_id=unit_id,
                from_representation=old.representation,
                to_projection=projection,
            )
        )
    advanced = {
        item.unit_id: advance_projection_generation(
            item,
            projection_generation=to_generation,
        )
        for item in current_state.unit_projections
    }
    advanced.update({entry.unit_id: entry.to_projection for entry in entries})
    final_units = tuple(
        advanced[item.unit_id] for item in current_state.unit_projections
    )
    final_state = build_projection_state(
        window=window,
        projection_generation=to_generation,
        through_sequence=source_through_sequence,
        unit_projections=final_units,
        rollups=rollups,
    )
    reason = (
        ProjectionRewriteReason.ROLLUP_CREATED
        if prepared_rollups
        else ProjectionRewriteReason.SOFT_TARGET_EXCEEDED
    )
    plan_payload = {
        "window_id": window.window_id,
        "from_projection_generation": current_state.projection_generation,
        "to_projection_generation": to_generation,
        "source_through_sequence": source_through_sequence,
        "entries": tuple(entries),
        "rollups": rollups,
        "reason_code": reason,
        "final_state_fingerprint": final_state.state_semantic_fingerprint,
    }
    plan_fingerprint = context_fingerprint(
        "context-projection-rewrite-plan:v1", plan_payload
    )
    rewrite_id = f"context_projection_rewrite:{plan_fingerprint.removeprefix('sha256:')}"
    page_count = ceil(len(entries) / max_entries_per_page)
    events = tuple(
        ContextProjectionRewritePageEvent(
            id=f"{rewrite_id}:page:{page_index}",
            **event_context.event_fields(),
            rewrite_id=rewrite_id,
            window_id=window.window_id,
            from_projection_generation=current_state.projection_generation,
            to_projection_generation=to_generation,
            source_through_sequence=source_through_sequence,
            page_index=page_index,
            page_count=page_count,
            entries=tuple(
                entries[
                    page_index
                    * max_entries_per_page : (page_index + 1)
                    * max_entries_per_page
                ]
            ),
            rollups=rollups if page_index == 0 else (),
            plan_fingerprint=plan_fingerprint,
            final_state_fingerprint=final_state.state_semantic_fingerprint,
            reason_code=reason,
        )
        for page_index in range(page_count)
    )
    return ContextProjectionRewritePlan(
        rewrite_id=rewrite_id,
        source_through_sequence=source_through_sequence,
        events=events,
        final_state=final_state,
        plan_fingerprint=plan_fingerprint,
        prepared_rollup_artifacts=prepared_rollups,
    )


__all__ = [
    "ContextProjectionRewritePlan",
    "CurrentRunProjectionPlanningInput",
    "ProjectionPlanningResult",
    "ProjectionTargetUnreachable",
    "projection_target_unreachable_audit",
    "plan_deterministic_projection_rewrite",
    "plan_new_result_ingest",
    "prepare_current_run_projection_planning_input",
]
