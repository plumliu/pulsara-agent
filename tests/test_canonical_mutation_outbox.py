from pulsara_agent.memory.canonical.mutation_outbox import (
    CanonicalMutationSurface,
    CanonicalMutationSurfaceState,
    graph_reset_mutation_payload,
    runtime_semantic_mutation_payload,
)


def test_runtime_semantic_outbox_defaults_only_to_explicit_oxigraph_surface() -> None:
    payload = runtime_semantic_mutation_payload(
        node_id="timeline:test",
        document={"@id": "timeline:test", "@type": ["RunTimelineRecord"]},
        source_runtime_session_id="runtime:test",
        source_run_id="run:test",
        source_turn_id="turn:test",
        source_reply_id="reply:test",
    )

    assert payload.surface_apply_status == {
        CanonicalMutationSurface.OXIGRAPH.value: CanonicalMutationSurfaceState.PENDING.value
    }


def test_mutation_payload_only_registers_surfaces_explicitly_passed_by_caller() -> None:
    payload = graph_reset_mutation_payload(
        async_surfaces=(
            CanonicalMutationSurface.OXIGRAPH.value,
            CanonicalMutationSurface.SEARCH_INDEX.value,
        )
    )

    assert payload.surface_apply_status == {
        CanonicalMutationSurface.OXIGRAPH.value: CanonicalMutationSurfaceState.PENDING.value,
        CanonicalMutationSurface.SEARCH_INDEX.value: CanonicalMutationSurfaceState.PENDING.value,
    }
