"""Measured gain uses comparable raw estimates and actual hard boundaries."""

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

import pulsara_agent.conversation_kernel.compaction.coordinator as coordinator
from pulsara_agent.conversation_kernel.compaction.planner import (
    CompactionPlanningError, CompactionReclaimUnavailable,
)
from pulsara_agent.llm.request import (
    FrozenProviderWireInputQuote, MAXIMUM_PROVIDER_WIRE_INPUT_BYTES,
)
from pulsara_agent.model_input.contracts import (
    ModelInputCompileFailureKind, StructuredModelInputCompileError,
)


def _quote(raw=900, *, budget=1000, used=None, wire_bytes=1800, estimator="sha256:estimator"):
    return FrozenProviderWireInputQuote(
        wire_api="openai_chat_completions", estimator_fingerprint=estimator,
        effective_input_budget_tokens=budget,
        generic_wire_estimated_input_tokens=raw, generic_wire_visual_image_tokens=0,
        replaced_generic_wire_estimated_tokens=0, replay_wire_estimated_tokens=0,
        raw_final_wire_estimated_input_tokens=raw, final_wire_visual_image_tokens=0,
        final_wire_utf8_bytes=wire_bytes, budget_input_tokens=raw if used is None else used,
        budget_source="heuristic" if used is None else "reported_input_anchor",
        anchor_model_call_id=None if used is None else "accepted-provider-call",
        anchor_reported_input_tokens=used, estimated_suffix_tokens=None if used is None else 0,
    )


def _transition(monkeypatch, *, source=None, successor=None, switched=False):
    monkeypatch.setattr(coordinator, "_compaction_cut_lineage_exactly_joins", lambda **_: True)
    source = source or _quote()
    successor = successor or _quote(899, wire_bytes=1900)
    estimator = NS(fact="same-estimator")
    source_target = NS(fact="A", model_profile=NS(route_wire_profile=NS(wire_api=source.wire_api)), token_estimator=estimator)
    target = NS(fact="B", model_profile=source_target.model_profile, token_estimator=estimator) if switched else source_target
    request_binding = object()
    identity = object()
    source_binding = NS(target_fact=source_target.fact, estimator=estimator, estimator_fingerprint=source.estimator_fingerprint, effective_input_budget_tokens=source.effective_input_budget_tokens, binding_fingerprint="source-binding", tool_surface=NS(tool_specs=()))
    successor_binding = NS(target_fact=target.fact, estimator=estimator, estimator_fingerprint=successor.estimator_fingerprint, effective_input_budget_tokens=successor.effective_input_budget_tokens)
    read = NS(compile_snapshot=NS(canonical_input=NS(identity=identity)))
    semantic = NS(canonical_input_identity=identity, compile_binding_fingerprint="source-binding", system_prompt="system", messages=(), tools=())
    source_candidate = NS(canonical_read=read, semantic_input=semantic, prepared_call=NS(call=NS(target=source_target, binding=request_binding), compile_binding=source_binding))
    successor_candidate = NS(canonical_read=read, semantic_input=NS(canonical_input_identity=identity), prepared_call=NS(call=NS(target=target, binding=request_binding), compile_binding=successor_binding))
    return dict(
        source_view=NS(provider_wire_quote=source, canonical_dispatch_read=read, normal_compile_binding=source_binding, materialized_system_prompt=lambda: "system", materialized_messages=lambda: ()),
        source_candidate=source_candidate,
        successor_wire=NS(candidate=successor_candidate, quote=successor, wire_input_plan=object()),
        phase="PRE_FULL",
        destination_target=NS(target=target, call=NS(binding=request_binding)) if switched or source.effective_input_budget_tokens != successor.effective_input_budget_tokens else None,
    )


def test_one_raw_token_gain_can_adopt_with_larger_wire_and_above_old_landing(monkeypatch):
    result = coordinator.validate_compaction_wire_transition(**_transition(monkeypatch))
    assert result.reclaim_tokens == 1
    assert result.successor_quote.budget_input_tokens == 899
    assert result.successor_quote.final_wire_utf8_bytes > result.source_quote.final_wire_utf8_bytes


def test_reported_usage_anchor_does_not_supply_artificial_gain(monkeypatch):
    args = _transition(monkeypatch, source=_quote(800, used=990), successor=_quote(810))
    with pytest.raises(CompactionReclaimUnavailable):
        coordinator.validate_compaction_wire_transition(**args)
    args = _transition(monkeypatch, source=_quote(900, used=700), successor=_quote(899))
    assert coordinator.validate_compaction_wire_transition(**args).reclaim_tokens == 1


@pytest.mark.parametrize("switched", [False, True])
def test_target_or_legal_budget_handoff_requires_only_destination_hardfit(monkeypatch, switched):
    args = _transition(monkeypatch, source=_quote(100), successor=_quote(950, budget=1001), switched=switched)
    assert coordinator.validate_compaction_wire_transition(**args).reclaim_tokens == 0


def test_postfull_accepts_new_material_without_reapplying_ordinary_gain(monkeypatch):
    args = _transition(monkeypatch, source=_quote(900), successor=_quote(999, wire_bytes=2000))
    args["phase"] = "POST_FULL"
    assert coordinator.validate_compaction_wire_transition(**args).reclaim_tokens == 0


@pytest.mark.parametrize("phase", ["PRE_FULL", "POST_FULL"])
def test_413_uses_original_bytes_even_if_tokens_increase(monkeypatch, phase):
    args = _transition(monkeypatch, source=_quote(400, wire_bytes=1800), successor=_quote(999, wire_bytes=1799))
    args.update(phase=phase, failed_wire_quote=args["source_view"].provider_wire_quote)
    assert coordinator.validate_compaction_wire_transition(**args).reclaim_tokens == 0
    args["successor_wire"].quote = replace(args["successor_wire"].quote, final_wire_utf8_bytes=1800)
    with pytest.raises(CompactionReclaimUnavailable, match="failed request"):
        coordinator.validate_compaction_wire_transition(**args)


def test_known_local_wire_recovery_uses_real_bytes(monkeypatch):
    args = _transition(monkeypatch, source=_quote(400, wire_bytes=MAXIMUM_PROVIDER_WIRE_INPUT_BYTES + 1), successor=_quote(999))
    assert coordinator.validate_compaction_wire_transition(**args).reclaim_tokens == 0


@pytest.mark.parametrize("over_budget", [False, True])
def test_successor_hard_admission_is_required_in_both_phases(monkeypatch, over_budget):
    quote = _quote(1001) if over_budget else _quote(950, wire_bytes=MAXIMUM_PROVIDER_WIRE_INPUT_BYTES + 1)
    args = _transition(monkeypatch, successor=quote)
    args["successor_wire"].wire_input_plan = None
    for phase in ("PRE_FULL", "POST_FULL"):
        args["phase"] = phase
        with pytest.raises(coordinator.CompactionSuccessorHardAdmissionError):
            coordinator.validate_compaction_wire_transition(**args)


def test_drift_fails_before_numerical_benefit(monkeypatch):
    args = _transition(monkeypatch)
    args["source_candidate"].semantic_input.messages = ("changed",)
    with pytest.raises(coordinator.CompactionWireTransitionDrift):
        coordinator.validate_compaction_wire_transition(**args)


def test_retention_retries_only_follow_typed_successor_hard_admission():
    predicate = coordinator._can_retry_compaction_with_smaller_tail
    assert predicate(coordinator.CompactionSuccessorHardAdmissionError("hard model budget"))
    assert predicate(StructuredModelInputCompileError(ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET))
    assert not predicate(CompactionReclaimUnavailable("no raw-token improvement"))
    assert not predicate(CompactionPlanningError("arbitrary planning failure"))
    assert not predicate(coordinator.CompactionWireTransitionDrift("cut drift"))
    assert not predicate(RuntimeError("provider 413"))


def test_destination_summary_already_using_b_still_has_cross_target_semantics(monkeypatch):
    # Source planning can already serialize the old history for B. The operation
    # owner still knows this is a model handoff rather than an A/B token comparison.
    args = _transition(monkeypatch, source=_quote(100), successor=_quote(999))
    args["require_token_reclaim"] = False
    assert coordinator.validate_compaction_wire_transition(**args).reclaim_tokens == 0


def _lineage_candidates(*, post=False):
    from pulsara_agent.conversation_kernel.compaction.contracts import CompactionContinuationMode
    from pulsara_agent.conversation_kernel.compaction.prompt import build_compaction_snapshot_carrier, freeze_compaction_summary_output
    from pulsara_agent.identity import derive_id
    from pulsara_agent.llm.input import LLMTextPart
    from pulsara_agent.model_input.contracts import (
        CanonicalInputOriginKind, ContextBindingBaseKind,
        FrozenProviderInputItem, FrozenProviderInputItemKind,
    )

    def ident(kind, role):
        return derive_id(kind, "transition-lineage", role)

    def original(role, sequence):
        return FrozenProviderInputItem(
            FrozenProviderInputItemKind.USER, ident("entry", role), sequence,
            ident("turn", "current"), (LLMTextPart(role),),
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        )

    active, summarized, new = original("active", 2), original("old-tool-followup", 12), original("new", 16)
    summary = freeze_compaction_summary_output("summary", maximum_utf8_bytes=100)

    def snapshot(cut):
        return FrozenProviderInputItem(
            FrozenProviderInputItemKind.CONTEXT_SNAPSHOT, None, cut, None,
            build_compaction_snapshot_carrier(
                summary=summary, retained_entry_ids=(active.source_entry_id,),
                continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
                active_request_entry_id=active.source_entry_id,
            ),
        )

    def candidate(cut, revision, items, through):
        binding_id = ident("binding", str(revision))
        identity = NS(session_id=ident("session", "current"), turn_id=ident("turn", "current"), initial_entry_id=active.source_entry_id, context_binding_revision_id=binding_id, provider_input_through_sequence=through, conversation_scope_kind="ROOT")
        binding = NS(base_kind=ContextBindingBaseKind.SNAPSHOT, context_snapshot_id=ident("snapshot", str(revision)), revision_ordinal=revision, binding_revision_id=binding_id, source_through_sequence=cut)
        canonical_input = NS(identity=identity, items=items, canonical_expanded_bytes=sum(
            item.content.canonical_expanded_bytes if item.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT else coordinator.provider_input_item_canonical_expanded_bytes(item)
            for item in items
        ), closures=(), late_outcomes=())
        facts = NS(canonical_input=canonical_input, context_binding_fact=binding, run_permission_snapshot=None, plan_workflow_fact=None, plan_handoff_fact=None, previous_turn_outcome_fact=None)
        return NS(canonical_read=NS(compile_snapshot=facts, replay_manifest_cut=NS(manifests=())))

    source = candidate(10, 3, (snapshot(10), active, summarized, new), 16)
    successor_items = (snapshot(15), active, new)
    if post:
        successor_items += (original("steer", 18),)
    return source, candidate(15, 4, successor_items, 18 if post else 16)


@pytest.mark.parametrize("post", [False, True])
def test_lineage_preserves_active_original_before_prior_floor_and_new_suffix(post):
    source, successor = _lineage_candidates(post=post)
    assert coordinator._compaction_cut_lineage_exactly_joins(
        source_candidate=source, successor_candidate=successor,
        phase="POST_FULL" if post else "PRE_FULL",
    )
    if post:
        assert not coordinator._compaction_cut_lineage_exactly_joins(
            source_candidate=source, successor_candidate=successor, phase="PRE_FULL",
        )


@pytest.mark.parametrize("change", ["missing", "body", "unselected", "bytes"])
def test_lineage_rejects_missing_or_changed_referenced_original(change):
    from pulsara_agent.llm.input import LLMTextPart
    source, successor = _lineage_candidates()
    canonical = successor.canonical_read.compile_snapshot.canonical_input
    snapshot, active, new = canonical.items
    if change == "missing":
        canonical.items = (snapshot, new)
    elif change == "body":
        canonical.items = (snapshot, replace(active, content=(LLMTextPart("changed"),)), new)
    elif change == "unselected":
        canonical.items = (snapshot, active, source.canonical_read.compile_snapshot.canonical_input.items[2], new)
    else:
        canonical.canonical_expanded_bytes -= 1
    assert not coordinator._compaction_cut_lineage_exactly_joins(
        source_candidate=source, successor_candidate=successor, phase="PRE_FULL",
    )
