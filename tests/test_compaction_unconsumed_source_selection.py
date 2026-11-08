"""Unconsumed accepted request rows stay outside historical compaction input."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.identity import derive_id
from pulsara_agent.conversation_kernel.compaction.planner import (
    unconsumed_request_start_sequence,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItemKind,
)
from tests.test_compaction_first_call_source_boundary import _first_call_read


@pytest.mark.parametrize(
    "origin",
    (
        CanonicalInputOriginKind.HUMAN_STEER,
        CanonicalInputOriginKind.AGENT_MESSAGE,
        CanonicalInputOriginKind.AGENT_MATERIAL,
        CanonicalInputOriginKind.CELL_OUTPUT,
        CanonicalInputOriginKind.USER_CONTROL_FEEDBACK,
    ),
)
def test_text_supplement_is_n_until_an_assistant_consumes_its_input_cut(origin):
    read = _first_call_read()
    canonical = read.dispatch_read.compile_snapshot.canonical_input
    old, answer, context, instruction, dependency = canonical.items
    supplement = replace(context, input_origin=origin)
    source = SimpleNamespace(items=(old, answer, supplement, instruction, dependency))
    assert unconsumed_request_start_sequence(source, consumed_through_sequence=2) == 3
    assert (
        unconsumed_request_start_sequence(source, consumed_through_sequence=5) is None
    )
    assert old.source_entry_sequence == 1  # The active/old request is not reclassified.


@pytest.mark.parametrize(
    "kind",
    (
        FrozenProviderInputItemKind.PLAN_CONTINUATION,
        FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
    ),
)
def test_new_non_human_request_material_has_the_same_n_boundary(kind):
    canonical = _first_call_read().dispatch_read.compile_snapshot.canonical_input
    item = replace(
        canonical.items[2],
        item_kind=kind,
        input_origin=(
            CanonicalInputOriginKind.CELL_OUTPUT
            if kind is FrozenProviderInputItemKind.TERMINAL_OBSERVATION
            else CanonicalInputOriginKind.PLAN_CONTINUATION
        ),
    )
    assert (
        unconsumed_request_start_sequence(
            SimpleNamespace(items=(item,)), consumed_through_sequence=2
        )
        == 3
    )


def test_imported_settled_request_is_not_a_new_delivery():
    canonical = _first_call_read().dispatch_read.compile_snapshot.canonical_input
    imported = replace(
        canonical.items[2], source_turn_id=derive_id("history", "settled")
    )
    assert (
        unconsumed_request_start_sequence(
            SimpleNamespace(items=(imported,)), consumed_through_sequence=2
        )
        is None
    )


def test_first_call_uses_its_existing_whole_group_ceiling():
    canonical = _first_call_read().dispatch_read.compile_snapshot.canonical_input
    assert (
        unconsumed_request_start_sequence(canonical, consumed_through_sequence=None)
        is None
    )


@pytest.mark.parametrize("active,expected_read_cut", ((True, 2), (False, 5)))
def test_source_selection_excludes_unconsumed_rows_only_for_active_turns(
    monkeypatch, active, expected_read_cut
):
    import asyncio
    from time import monotonic
    from pulsara_agent.conversation_kernel.compaction import coordinator as module
    from pulsara_agent.conversation_kernel.compaction.contracts import (
        CompactionTargetBranch,
        ResolvedCompactionPolicy,
    )
    from tests.test_compaction_first_call_source_boundary import _source

    read = _first_call_read()
    source, projection = _source(read)
    source.latest_assistant_input_through_sequence = 2
    captured = []

    def prepare(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(source_proof=SimpleNamespace(projection=None))

    async def measure(candidate, **kwargs):
        return SimpleNamespace(wire_input_plan=object())

    monkeypatch.setattr(module, "prepare_compaction_summary_semantic", prepare)
    owner = module.CompactionCoordinator.__new__(module.CompactionCoordinator)
    owner._model = SimpleNamespace(resolve_compaction_summary_call=lambda **_: object())
    owner._provider_dispatch = SimpleNamespace(measure_prepared_wire_candidate=measure)
    dispatch = SimpleNamespace(
        prepared_call=SimpleNamespace(
            call=SimpleNamespace(
                target=SimpleNamespace(
                    fact=SimpleNamespace(input_modalities=("text", "image"))
                )
            )
        ),
        projection=SimpleNamespace(projected_input=projection),
        tool_exposure_plan=SimpleNamespace(direct_projection_set=object()),
    )
    winner = asyncio.run(
        owner._select_summary(
            dispatch=dispatch,
            source_view=source,
            compaction_read=read,
            policy=ResolvedCompactionPolicy(),
            maximum_retained_tool_groups=None,
            target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION
            if active
            else CompactionTargetBranch.IDLE_BASE_ONLY,
            summary_request="Summarize H using R as reference.",
            deadline=monotonic() + 30,
        )
    )
    assert winner is not None
    assert len(captured) == 1
    assert captured[0]["read_through_sequence"] == expected_read_cut
    assert winner.prefix.source_through_sequence == expected_read_cut
    if active:
        current_ids = {
            item.source_entry_id
            for item in read.dispatch_read.compile_snapshot.canonical_input.items[2:]
        }
        assert current_ids.isdisjoint(winner.retained_entry_ids)


def test_hostile_projection_notice_does_not_interrupt_full_settlement(
    stage2_migrated_postgres_database, monkeypatch
):
    from pulsara_agent.conversation_kernel.compaction.coordinator import (
        CompactionCoordinator,
    )
    from tests.test_stage2_conversation_runner import (
        test_cold_text_only_handover_uses_one_destination_projection as exercise,
    )

    initialize = CompactionCoordinator.__init__
    delivered = []

    def initialize_with_hostile_sink(self, **kwargs):
        sink = kwargs.get("presentation_notice_sink")
        if sink is not None:

            def hostile(text):
                delivered.append(text)
                sink(text)
                raise RuntimeError("presentation recipient disappeared after FULL")

            kwargs["presentation_notice_sink"] = hostile
        initialize(self, **kwargs)

    monkeypatch.setattr(CompactionCoordinator, "__init__", initialize_with_hostile_sink)
    # This existing PG fixture verifies FULL adoption, the continued destination
    # business request, exactly one summary, original history and owner closure.
    exercise(stage2_migrated_postgres_database)
    assert len(delivered) == 1
