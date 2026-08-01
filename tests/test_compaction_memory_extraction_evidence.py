from __future__ import annotations

from pulsara_agent.event_log.historical_decoder import decode_raw_stored_event_envelope

from dataclasses import dataclass
from time import monotonic

import pytest

from pulsara_agent.event import EventContext, RunEndEvent, RunStartEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.compaction.evidence import (
    ExactHumanEvidenceSource,
    select_compaction_memory_extraction_input,
)
from pulsara_agent.memory.compaction.manifest import (
    CompactionHumanEvidenceManifestPlan,
    _eligible_human_leaf,
    build_human_evidence_manifest_plan,
)
from pulsara_agent.primitives._context_base import thaw_json
from pulsara_agent.primitives.compaction import (
    CompactionPostCompletionExtensionLinkFact,
    ResolvedExtractionInputBudgetAttributionFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    GovernanceStoredEventReferenceFact,
)
from pulsara_agent.runtime.authority_materialization.transcript_reducer import (
    GovernanceTranscriptAuthoritySnapshot,
    TranscriptProjectionDocumentRegistry,
    TranscriptProjectionStateStore,
)
from pulsara_agent.runtime.projection_jobs.source import source_event_reference
from tests.conftest import run_end_contract_fields, run_start_permission_fields
from tests.support.event_write import restore_transcript_projection_fixture


@dataclass(frozen=True)
class _EvidenceWorld:
    runtime_session_id: str
    log: InMemoryEventLog
    plan: CompactionHumanEvidenceManifestPlan
    archive: InMemoryArchiveStore
    authority: GovernanceTranscriptAuthoritySnapshot


def _run_pair(*, index: int, text: str, source: str, identity_namespace: str):
    context = EventContext(
        run_id=f"run:evidence-source:{identity_namespace}:{index}",
        turn_id=f"turn:evidence-source:{identity_namespace}:{index}",
        reply_id=f"reply:evidence-source:{identity_namespace}:{index}",
    )
    fields = run_start_permission_fields(
        context.run_id,
        source="child_profile" if source == "subagent" else "session_default",
        user_input=text,
    )
    start = RunStartEvent(
        id=f"run_start:test:{context.run_id}",
        **context.event_fields(),
        **fields,
        user_input_chars=len(text),
        metadata={"user_input": text},
    )
    end = RunEndEvent(
        **context.event_fields(),
        **run_end_contract_fields(context.run_id, status="finished"),
        status="finished",
        stop_reason="final",
    )
    return start, end


def _world(
    messages: tuple[tuple[str, str], ...],
    *,
    runtime_session_id: str = "runtime:compaction-memory-evidence",
) -> _EvidenceWorld:
    log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    committed = []
    identity_namespace = runtime_session_id.rsplit(":", 1)[-1]
    for index, (source, text) in enumerate(messages):
        committed.extend(
            log.extend(
                _run_pair(
                    index=index,
                    text=text,
                    source=source,
                    identity_namespace=identity_namespace,
                )
            )
        )
    reducer = TranscriptProjectionStateStore(
        runtime_session_id=runtime_session_id,
        documents=TranscriptProjectionDocumentRegistry(),
    )
    restore_transcript_projection_fixture(event_log=log, reducer=reducer)
    authority = reducer.capture_governance_authority_snapshot()
    plan = build_human_evidence_manifest_plan(
        runtime_session_id=runtime_session_id,
        authority_snapshot=authority,
        previous_keep_after_sequence=0,
        current_keep_after_sequence=authority.ledger_through_sequence,
        current_through_sequence=authority.ledger_through_sequence,
        predecessor_completed_event_id=None,
        event_lookup=log.get_by_id,
    )
    archive = InMemoryArchiveStore()
    for artifact in plan.artifacts:
        archive.put_text_if_absent_or_confirm_identical(
            artifact.reference.artifact_id,
            artifact.content,
            session_id=runtime_session_id,
            run_id=None,
            media_type=artifact.reference.media_type,
            semantic_metadata=thaw_json(artifact.semantic_metadata),
            deadline_monotonic=monotonic() + 10.0,
        )
    return _EvidenceWorld(
        runtime_session_id=runtime_session_id,
        log=log,
        plan=plan,
        archive=archive,
        authority=authority,
    )


def _select(
    world: _EvidenceWorld,
    *,
    usable_tokens: int = 100_000,
    token_estimator=lambda value: max(1, len(value.encode("utf-8")) // 4),
    maximum_nodes: int = 256,
    resolver_transform=lambda value: value,
    request_reference_override=None,
    durable_source_reference_override=None,
    extension_link_override=None,
):
    raw = world.log.read_raw_events_by_id(
        tuple(event.id for event in world.log.iter()),
        deadline_monotonic=monotonic() + 10.0,
    )
    raw_by_id = {item.event_id: item for item in raw}
    run_starts = tuple(item for item in raw if item.event_type == "RUN_START")
    exact_reads = 0

    def resolve(reference):
        nonlocal exact_reads
        exact_reads += 1
        envelope = raw_by_id[reference.event_id]
        event = decode_raw_stored_event_envelope(
            envelope, DEFAULT_EVENT_SCHEMA_REGISTRY
        )
        stored = build_frozen_fact(
            GovernanceStoredEventReferenceFact,
            schema_version="governance_stored_event_reference.v1",
            stable_identity=stable_event_identity(
                event,
                runtime_session_id=world.runtime_session_id,
            ),
            sequence=envelope.sequence,
            stored_envelope_fingerprint=envelope.envelope_fingerprint,
        )
        return resolver_transform(
            ExactHumanEvidenceSource(event=event, stored_reference=stored)
        )

    request_reference = resolve(
        world.plan.pages[0].ordered_leaf_attributions[0].exact_run_start_event_reference
    ).stored_reference
    exact_reads = 0
    budget = build_frozen_fact(
        ResolvedExtractionInputBudgetAttributionFact,
        schema_version="resolved_extraction_input_budget_attribution.v1",
        resolved_model_target_fingerprint="sha256:test-target",
        target_input_limit_tokens=usable_tokens + 1,
        static_prompt_tokens=0,
        carrier_and_framing_reserve_tokens=0,
        output_reserve_tokens=1,
        safety_margin_tokens=0,
        usable_evidence_tokens=usable_tokens,
        maximum_physical_input_utf8_bytes=512 * 1024,
        token_estimator_contract_fingerprint="sha256:test-estimator",
        budget_selection_contract_fingerprint="sha256:test-budget",
    )
    link = extension_link_override or build_frozen_fact(
        CompactionPostCompletionExtensionLinkFact,
        schema_version="compaction_post_completion_extension_link.v1",
        compaction_id="compaction:evidence",
        completed_event_id="completed:evidence",
        request_event_id="request:evidence",
        extension_contract_fingerprint="sha256:test-extension",
    )
    selected = select_compaction_memory_extraction_input(
        runtime_session_id=world.runtime_session_id,
        compaction_id=link.compaction_id,
        extension_link=link,
        request_event_reference=request_reference_override or request_reference,
        durable_job_id="projection-job:evidence",
        durable_job_source_reference=(
            durable_source_reference_override or source_event_reference(run_starts[0])
        ),
        manifest_reference=world.plan.reference,
        archive=world.archive,
        exact_source_resolver=resolve,
        resolved_budget=budget,
        token_estimator=token_estimator,
        prompt_contract_fingerprint="sha256:test-prompt",
        extraction_contract_fingerprint="sha256:test-extraction",
        deadline_monotonic=monotonic() + 10.0,
        maximum_nodes=maximum_nodes,
    )
    return selected, exact_reads


def test_manifest_and_selector_include_only_direct_human_input() -> None:
    world = _world(
        (
            ("human", "Remember that I prefer compact reports."),
            ("subagent", "Parent-generated child task."),
        )
    )

    selected, exact_reads = _select(world)

    assert world.plan.semantic.eligible_leaf_count == 1
    assert exact_reads == 1
    assert tuple(
        item.semantic.sanitized_full_message_text for item in selected.ordered_nodes
    ) == ("Remember that I prefer compact reports.",)

    human_entry = world.authority.reducer_evidence_snapshot.stable_entries[0]
    provider = human_entry.semantic_identity.message_provider_semantic_identity
    runtime_entry = human_entry.model_copy(
        update={
            "semantic_identity": human_entry.semantic_identity.model_copy(
                update={
                    "message_provider_semantic_identity": provider.model_copy(
                        update={
                            "role": "runtime_request",
                            "name": "terminal_process_observation",
                        }
                    )
                }
            )
        }
    )
    assert (
        _eligible_human_leaf(
            entry=runtime_entry,
            previous_keep_after_sequence=0,
            current_keep_after_sequence=(world.authority.ledger_through_sequence),
            event_lookup=world.log.get_by_id,
            runtime_session_id=world.runtime_session_id,
        )
        is None
    )


def test_selector_scans_long_manifest_but_exact_reads_at_most_256() -> None:
    world = _world(tuple(("human", f"preference {index}") for index in range(300)))

    selected, exact_reads = _select(world)

    assert world.plan.root.page_count >= 2
    assert selected.source_eligible_leaf_count == 300
    assert len(selected.ordered_nodes) == 256
    assert exact_reads == 256
    assert selected.permanent_omission_count == 44
    assert (
        selected.ordered_nodes[0].semantic.sanitized_full_message_text
        == "preference 44"
    )
    assert (
        selected.ordered_nodes[-1].semantic.sanitized_full_message_text
        == "preference 299"
    )


def test_selector_skips_newer_nonfitting_leaf_and_backfills_older() -> None:
    world = _world(
        (
            ("human", "keep this"),
            ("human", "x" * 200),
        )
    )

    selected, exact_reads = _select(
        world,
        usable_tokens=20,
        token_estimator=len,
    )

    assert exact_reads == 1
    assert tuple(
        item.semantic.sanitized_full_message_text for item in selected.ordered_nodes
    ) == ("keep this",)
    assert selected.permanent_omission_count == 1


def test_selector_rejects_stored_authority_cross_pair() -> None:
    world = _world((("human", "Remember exact evidence."),))

    def drift(source: ExactHumanEvidenceSource) -> ExactHumanEvidenceSource:
        stored = source.stored_reference
        wrong = build_frozen_fact(
            GovernanceStoredEventReferenceFact,
            schema_version="governance_stored_event_reference.v1",
            stable_identity=stored.stable_identity,
            sequence=stored.sequence,
            stored_envelope_fingerprint="sha256:wrong-envelope",
        )
        return ExactHumanEvidenceSource(event=source.event, stored_reference=wrong)

    with pytest.raises(ValueError, match="stored authority rebind"):
        _select(world, resolver_transform=drift)
