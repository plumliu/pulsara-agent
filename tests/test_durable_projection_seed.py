from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from pulsara_agent.event import EventContext, ReplyEndEvent
from pulsara_agent.event_log.in_memory import InMemoryEventLog
from pulsara_agent.event_log.transcript_prefix import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.projection_jobs.contracts import (
    DurableProjectionCommitConfirmation,
    DurableProjectionKind,
    DurableProjectionKindActivationFact,
    DurableProjectionKindActivationSemanticFact,
    DurableProjectionSeedFailureCommitCandidateFact,
    DurableProjectionSessionCutoverFact,
    build_projection_fact,
)
from pulsara_agent.runtime.projection_jobs.migration_state import (
    next_projection_migration_requirement,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    DURABLE_PROJECTION_TRIGGER_REGISTRY,
    DurableProjectionExecutableBinding,
    DurableProjectionExecutableRegistry,
)
from pulsara_agent.runtime.projection_jobs.service import (
    DurableProjectionJobService,
)
from pulsara_agent.runtime.publisher import RuntimePublishedEvent
from pulsara_agent.runtime.projection_jobs.source import (
    build_job_candidate,
    exact_stored_event,
    verify_job_source,
)


def test_exact_source_rebind_and_page_independent_job_identity() -> None:
    event_log = InMemoryEventLog(runtime_session_id="runtime:seed")
    stored_event = event_log.append(
        ReplyEndEvent(
            **EventContext("run:seed", "turn:seed", "reply:seed").event_fields(),
            model_terminal_outcome="completed",
        )
    )
    stored = exact_stored_event(
        event_log=event_log,
        event_id=stored_event.id,
    )
    first = build_job_candidate(
        stored=stored,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
        activation_fingerprint="sha256:" + "1" * 64,
        trigger_registry=DURABLE_PROJECTION_TRIGGER_REGISTRY,
    )
    second = build_job_candidate(
        stored=stored,
        projection_kind=DurableProjectionKind.RUN_TIMELINE,
        activation_fingerprint="sha256:" + "1" * 64,
        trigger_registry=DURABLE_PROJECTION_TRIGGER_REGISTRY,
    )
    assert first == second
    assert (
        first.job_semantic.trigger_horizon.through_sequence
        == first.job_semantic.source_event_reference.sequence
    )
    verify_job_source(event_log=event_log, candidate=first)


def test_migration_prerequisites_are_staged() -> None:
    database_target = "sha256:" + "2" * 64
    registry_prefix = "sha256:" + "3" * 64
    assert (
        next_projection_migration_requirement(
            current_head_version=4,
            target_head_version=8,
            database_target_fingerprint=database_target,
            current_registry_prefix_fingerprint=registry_prefix,
            legacy_surface_binding_plan_ready=False,
            timeline_coverage_ready=False,
            evidence_coverage_ready=False,
        )
        is None
    )
    v6 = next_projection_migration_requirement(
        current_head_version=5,
        target_head_version=8,
        database_target_fingerprint=database_target,
        current_registry_prefix_fingerprint=registry_prefix,
        legacy_surface_binding_plan_ready=False,
        timeline_coverage_ready=False,
        evidence_coverage_ready=False,
    )
    assert v6 is not None
    assert v6.next_migration_version == 6
    v7 = next_projection_migration_requirement(
        current_head_version=6,
        target_head_version=8,
        database_target_fingerprint=database_target,
        current_registry_prefix_fingerprint=registry_prefix,
        legacy_surface_binding_plan_ready=True,
        timeline_coverage_ready=False,
        evidence_coverage_ready=False,
    )
    assert v7 is not None
    assert v7.next_migration_version == 7
    v8 = next_projection_migration_requirement(
        current_head_version=7,
        target_head_version=8,
        database_target_fingerprint=database_target,
        current_registry_prefix_fingerprint=registry_prefix,
        legacy_surface_binding_plan_ready=True,
        timeline_coverage_ready=True,
        evidence_coverage_ready=False,
    )
    assert v8 is not None
    assert v8.next_migration_version == 8


def test_service_seed_cursor_wraps_and_isolates_one_authority_failure() -> None:
    kind = DurableProjectionKind.RUN_TIMELINE
    seed_contract = DURABLE_PROJECTION_TRIGGER_REGISTRY.resolve(kind)
    semantic = cast(
        DurableProjectionKindActivationSemanticFact,
        build_projection_fact(
            DurableProjectionKindActivationSemanticFact,
            schema_version=(
                "durable_projection_kind_activation_semantic.v1"
            ),
            activation_id="activation:test:timeline",
            projection_kind=kind,
            seed_contract=seed_contract,
            activation_policy="post_cutover_events_only",
        ),
    )
    activation = cast(
        DurableProjectionKindActivationFact,
        build_projection_fact(
            DurableProjectionKindActivationFact,
            schema_version="durable_projection_kind_activation.v1",
            activation_semantic=semantic,
            activation_migration_version=7,
            resulting_migration_registry_prefix_fingerprint=context_fingerprint(
                "test-projection-registry-prefix:v1", 7
            ),
        ),
    )
    cutovers = tuple(
        cast(
            DurableProjectionSessionCutoverFact,
            build_projection_fact(
                DurableProjectionSessionCutoverFact,
                schema_version="durable_projection_session_cutover.v1",
                runtime_session_id=f"runtime:{index:04d}",
                projection_kind=kind,
                cutover_through_sequence=0,
                cutover_ledger_continuity_accumulator=(
                    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
                ),
                cutover_ledger_payload_prefix_bytes=0,
                cutover_transcript_semantic_prefix_count=0,
                cutover_transcript_semantic_prefix_accumulator=(
                    EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR
                ),
                migration_version=7,
                migration_registry_prefix_fingerprint=(
                    activation.resulting_migration_registry_prefix_fingerprint
                ),
                activation_fingerprint=activation.activation_fingerprint,
                seed_contract_fingerprint=(
                    seed_contract.seed_contract_fingerprint
                ),
                cutover_policy_id="post_cutover_events_only",
            ),
        )
        for index in range(257)
    )

    class Repository:
        visited: list[str] = []
        failures: list[DurableProjectionSeedFailureCommitCandidateFact] = []

        def list_active_seed_authorities(
            self,
            *,
            after_runtime_session_id=None,
            after_projection_kind=None,
            limit,
            deadline_monotonic,
        ):
            del after_projection_kind, deadline_monotonic
            start = 0
            if after_runtime_session_id is not None:
                start = next(
                    index + 1
                    for index, item in enumerate(cutovers)
                    if item.runtime_session_id == after_runtime_session_id
                )
            return tuple(
                (activation, item) for item in cutovers[start : start + limit]
            )

        def prepare_next_seed_candidate(
            self,
            *,
            runtime_session_id,
            projection_kind,
            deadline_monotonic,
        ):
            del projection_kind, deadline_monotonic
            self.visited.append(runtime_session_id)
            if runtime_session_id == "runtime:0005":
                raise ValueError("deterministic source authority failure")
            return None

        def read_seed_state(
            self,
            runtime_session_id,
            projection_kind,
            *,
            deadline_monotonic,
        ):
            del runtime_session_id, projection_kind, deadline_monotonic
            return None

        def commit(self, *, candidate, deadline_monotonic):
            del deadline_monotonic
            assert isinstance(
                candidate,
                DurableProjectionSeedFailureCommitCandidateFact,
            )
            self.failures.append(candidate)
            return SimpleNamespace(
                confirmation=DurableProjectionCommitConfirmation.FULL
            )

    executable_registry = DurableProjectionExecutableRegistry(
        (
            DurableProjectionExecutableBinding(
                contract=seed_contract.handler_contract,
                executable=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
            ),
        )
    )
    service = DurableProjectionJobService(
        connection_provider=object(),  # type: ignore[arg-type]
        executable_registry=executable_registry,
    )
    repository = Repository()
    service.repository = repository  # type: ignore[assignment]

    assert service._seed_cycle_blocking() is True
    assert "runtime:0255" in repository.visited
    assert "runtime:0256" not in repository.visited
    assert len(repository.failures) == 1
    assert repository.failures[0].runtime_session_id == "runtime:0005"
    assert service._seed_scan_continuation_pending is True
    assert service._seed_cycle_blocking() is False
    assert "runtime:0256" in repository.visited
    assert service._seed_scan_continuation_pending is False
    assert service._seed_cycle_blocking() is True
    assert repository.visited.count("runtime:0000") == 2


def test_published_trigger_enqueues_exact_dirty_authority_hint() -> None:
    kind = DurableProjectionKind.RUN_TIMELINE
    seed_contract = DURABLE_PROJECTION_TRIGGER_REGISTRY.resolve(kind)
    executable_registry = DurableProjectionExecutableRegistry(
        (
            DurableProjectionExecutableBinding(
                contract=seed_contract.handler_contract,
                executable=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
            ),
        )
    )
    service = DurableProjectionJobService(
        connection_provider=object(),  # type: ignore[arg-type]
        executable_registry=executable_registry,
    )
    service._accepting = True
    event = ReplyEndEvent(
        **EventContext("run:dirty", "turn:dirty", "reply:dirty").event_fields(),
        sequence=7,
        model_terminal_outcome="completed",
    )

    import asyncio

    asyncio.run(
        service.on_published_event(
            RuntimePublishedEvent(
                runtime_session_id="runtime:dirty",
                event=event,
            )
        )
    )

    assert tuple(service._dirty_authority_hints) == (
        ("runtime:dirty", DurableProjectionKind.RUN_TIMELINE),
    )
