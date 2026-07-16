"""Exact historical reconstruction of a durable context-input manifest."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from time import monotonic

from pulsara_agent.event import (
    CapabilityExposureResolvedEvent,
    ContextCompactionCompletedEvent,
    ContextCompiledEvent,
    ContextProjectionRewritePageEvent,
    ContextWindowCompactionCompletedEvent,
    ContextWindowClosedEvent,
    ContextWindowOpenedEvent,
    EventType,
    RolloutBudgetAccountOpenedEvent,
    RunStartEvent,
    SubagentRunCompletedEvent,
)
from pulsara_agent.event_log import EventLog
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.llm.models import ModelProfile, ModelRole
from pulsara_agent.llm.provider import ModelIdentityPolicy, ProviderProfile
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.llm.resolution import ResolvedModelCall, ResolvedModelTarget
from pulsara_agent.primitives.context import (
    ContextArtifactToolSchemaFact,
    ContextCompileInputAuditFact,
    ContextCompileInputManifestFact,
    ContextInlineToolSchemaFact,
    ContextMaterializedToolSpecInput,
    FrozenJsonObjectFact,
    PreparedContextCandidateSet,
    WindowCompactionSourceDocumentFact,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.long_horizon import (
    SubagentGraphAccelerationFact,
    default_subagent_graph_checkpoint_policy,
)
from pulsara_agent.primitives.tool_result import PreparedToolResultRenderInput
from pulsara_agent.runtime.context_input.event_slice import (
    ContextEventAuthorityView,
    ContextEventSlice,
    FrozenStoredEvent,
)
from pulsara_agent.runtime.context_input.event_slice import ContextEventSliceError
from pulsara_agent.runtime.context_input.render import prepare_tool_result_render_input
from pulsara_agent.runtime.context_input.render import (
    apply_tool_observation_projection,
    render_prepared_tool_result_units,
)
from pulsara_agent.runtime.context_input.manifest import (
    build_projected_tool_result_compile_refs,
)
from pulsara_agent.runtime.context_input.snapshot import (
    ContextCandidateSelectionMismatch,
    ContextFactSnapshot,
    ContextSnapshotBuildInput,
    build_context_snapshot,
    collect_replay_context_inputs,
)
from pulsara_agent.runtime.context_input.compiler import (
    canonical_render_decisions_fingerprint,
    compile_context_from_facts,
    provider_neutral_payload_fingerprint,
)
from pulsara_agent.runtime.context_input.transcript import (
    NormalizedContextTranscript,
)
from pulsara_agent.runtime.context_input.stable_transcript import (
    project_stable_context_transcript,
    required_terminal_content_artifacts,
)
from pulsara_agent.runtime.context_input.candidate import (
    ContextCandidateCollectionInput,
    build_context_candidate_authorities,
    collect_context_candidates,
)
from pulsara_agent.runtime.context_input.live import (
    collect_context_projection_references,
)
from pulsara_agent.runtime.plan import PLAN_ACTIVE_INSTRUCTION
from pulsara_agent.runtime.long_horizon.context_budget import (
    long_horizon_context_diagnostics,
    measure_long_horizon_context_budget,
)
from pulsara_agent.runtime.long_horizon.rollup import (
    default_observation_rollup_renderer_registry,
    prepare_observation_rollup_artifact,
)
from pulsara_agent.runtime.long_horizon.store import LongHorizonStateStore
from pulsara_agent.runtime.long_horizon.projection_reducer import (
    ContextWindowProjectionReducer,
)
from pulsara_agent.runtime.long_horizon.status import (
    derive_rollout_status_candidate_from_state,
    fold_sparse_rollout_state,
)
from pulsara_agent.runtime.authority_materialization import (
    build_default_authority_materialization_contract_bundle,
    build_default_transcript_projection_materialization_contracts,
    restore_transcript_projection_from_base,
)
from pulsara_agent.runtime.authority_materialization.contracts import (
    materialize_transcript_sparse_read_proof,
)


class ContextInputReplayStatus(StrEnum):
    EXACT_REPLAY = "exact_replay"
    FACT_REPLAY_ONLY = "fact_replay_only"
    ARTIFACT_MISSING = "artifact_missing"
    CONTRACT_MISMATCH = "contract_mismatch"
    LEDGER_UNTRUSTED = "ledger_untrusted"


class ContextInputReplayError(RuntimeError):
    def __init__(
        self,
        status: ContextInputReplayStatus,
        reason_code: str,
        message: str,
    ) -> None:
        self.status = status
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ReplayedContextInput:
    manifest: ContextCompileInputManifestFact
    snapshot_build_input: ContextSnapshotBuildInput
    normalized_transcript: NormalizedContextTranscript
    prepared_tool_results: PreparedToolResultRenderInput
    prepared_candidates: PreparedContextCandidateSet
    subagent_graph_acceleration: SubagentGraphAccelerationFact


@dataclass(frozen=True, slots=True)
class ReplayedCompiledContext:
    inputs: ReplayedContextInput
    compiled_context: object
    status: ContextInputReplayStatus = ContextInputReplayStatus.EXACT_REPLAY


@dataclass(frozen=True, slots=True)
class _ReplayOnlyTransport:
    api: str
    binding_id: str
    contract_version: str

    async def stream(self, **_kwargs):  # pragma: no cover - compile-only guard
        raise RuntimeError("replay-only transport cannot perform network I/O")


def load_context_input_manifest(
    *,
    audit: ContextCompileInputAuditFact,
    archive,
) -> ContextCompileInputManifestFact:
    """Load one manifest while preserving a stable replay failure class."""

    try:
        raw = archive.get_text(
            audit.input_manifest_artifact_id,
            session_id=audit.source_runtime_session_id,
        )
        info = archive.get_info(
            audit.input_manifest_artifact_id,
            session_id=audit.source_runtime_session_id,
        )
    except Exception as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.ARTIFACT_MISSING,
            "context_input_manifest_missing",
            "context input manifest is unavailable",
        ) from exc
    content_fingerprint = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        manifest = ContextCompileInputManifestFact.model_validate(json.loads(raw))
    except Exception as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_manifest_invalid",
            "context input manifest is invalid",
        ) from exc
    if (
        manifest.manifest_fingerprint != audit.input_manifest_fingerprint
        or manifest.input_aggregate_fingerprint != audit.input_aggregate_fingerprint
        or manifest.snapshot.snapshot_fact_fingerprint
        != audit.snapshot_fact_fingerprint
        or manifest.snapshot.snapshot_semantic_fingerprint
        != audit.snapshot_semantic_fingerprint
        or manifest.snapshot.long_horizon_attribution.attribution_fingerprint
        != audit.long_horizon_attribution_fingerprint
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_manifest_audit_mismatch",
            "context input manifest/audit identity mismatch",
        )
    metadata = info.metadata or {}
    if metadata.get("content_fingerprint") != content_fingerprint:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_manifest_content_hash_mismatch",
            "context input manifest content hash mismatch",
        )
    return manifest


def _validate_replayed_transcript_authority(
    *,
    manifest: ContextCompileInputManifestFact,
    archive,
    event_log: EventLog,
    event_slice: ContextEventSlice,
    named_slices: tuple[ContextEventSlice, ...],
) -> None:
    authority = manifest.transcript_authority
    base = authority.projection_base
    base_sequence = (
        base.common.run_seed_reference.source_ledger_through_sequence
        if base.base_kind == "run_seed"
        else base.checkpoint_acceleration.checkpoint_candidate_ledger_through_sequence
    )
    contracts = build_default_authority_materialization_contract_bundle()
    deadline = monotonic() + contracts.limits.operation_timeout_seconds
    try:
        delta = event_log.read_transcript_domain_delta(
            after_sequence=base_sequence,
            through_sequence=manifest.snapshot.identity.source_through_sequence,
            max_events=contracts.limits.max_unreclaimable_ledger_events,
            max_payload_bytes=(
                contracts.limits.max_unreclaimable_charged_payload_bytes
            ),
            registry_contract_fingerprint=(
                contracts.event_domain.contract.registry_contract_fingerprint
            ),
            deadline_monotonic=deadline,
        )
        proof = materialize_transcript_sparse_read_proof(
            delta,
            binding=contracts.event_domain,
        )
    except Exception as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_input_transcript_domain_proof_untrusted",
            "transcript semantic delta cannot be proven from the ledger",
        ) from exc
    expected_refs = tuple(
        (
            item.runtime_session_id,
            item.sequence,
            item.event_id,
            item.event_type,
            item.payload_fingerprint,
        )
        for item in authority.transcript_domain_delta_refs
    )
    actual_refs = tuple(
        (
            item.runtime_session_id,
            item.sequence,
            item.event_id,
            item.event_type,
            item.payload_fingerprint,
        )
        for item in delta.semantic_events
    )
    source = authority.semantic_source
    if (
        proof != authority.domain_completeness_proof
        or actual_refs != expected_refs
        or source.semantic_source_event_count != delta.after.semantic_event_count
        or source.semantic_source_accumulator != delta.after.semantic_accumulator
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_transcript_authority_mismatch",
            "ledger-derived transcript authority differs from the manifest",
        )

    all_slices = (event_slice, *named_slices)
    supplied = {
        (slice_.runtime_session_id, event.event_id): event
        for slice_ in all_slices
        for event in slice_.events
    }
    named_refs = tuple(
        ref
        for entry in authority.named_fact_selection.entries
        for ref in entry.source_refs
    )
    local_ids = tuple(
        sorted(
            {
                ref.event_id
                for ref in named_refs
                if ref.runtime_session_id
                == manifest.snapshot.identity.runtime_session_id
            }
        )
    )
    try:
        local_rows = event_log.read_raw_events_by_id(
            local_ids,
            deadline_monotonic=deadline,
        )
    except Exception as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_input_named_authority_untrusted",
            "named-fact authority events cannot be read",
        ) from exc
    local = {row.event_id: row for row in local_rows}
    for ref in named_refs:
        if ref.runtime_session_id == manifest.snapshot.identity.runtime_session_id:
            row = local.get(ref.event_id)
            actual = (
                row.sequence,
                row.event_type,
                row.payload_fingerprint,
            ) if row is not None else None
        else:
            event = supplied.get((ref.runtime_session_id, ref.event_id))
            actual = (
                event.sequence,
                event.event_type,
                event.payload_fingerprint,
            ) if event is not None else None
        if actual != (ref.sequence, ref.event_type, ref.payload_fingerprint):
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "context_input_named_authority_event_mismatch",
                "named-fact authority event differs from the manifest",
            )

    artifact_refs = tuple(
        ref
        for entry in authority.named_fact_selection.entries
        for ref in entry.source_artifact_refs
    )
    for ref in artifact_refs:
        try:
            info = archive.get_info(
                ref.artifact_id,
                session_id=manifest.snapshot.identity.runtime_session_id,
            )
            content = (
                archive.get_text(
                    ref.artifact_id,
                    session_id=manifest.snapshot.identity.runtime_session_id,
                ).encode("utf-8")
                if info.media_type.startswith("text/")
                or info.media_type in {"application/json", "application/xml"}
                else archive.get_bytes(
                    ref.artifact_id,
                    session_id=manifest.snapshot.identity.runtime_session_id,
                )
            )
        except Exception as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.ARTIFACT_MISSING,
                "context_input_named_authority_artifact_missing",
                "named-fact authority artifact is unavailable",
            ) from exc
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        metadata = info.metadata or {}
        contract = metadata.get("artifact_contract_fingerprint")
        if not isinstance(contract, str) or not contract.startswith("sha256:"):
            contract = context_fingerprint(
                "model-visible-named-fact-artifact-contract:v1",
                {"media_type": info.media_type},
            )
        if (
            ref.artifact_sha256 != digest
            or ref.artifact_byte_count != len(content)
            or ref.semantic_content_fingerprint != digest
            or ref.artifact_contract_fingerprint != contract
        ):
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "context_input_named_authority_artifact_mismatch",
                "named-fact authority artifact differs from the manifest",
            )


def replay_context_input(
    *,
    audit: ContextCompileInputAuditFact,
    archive,
    event_log: EventLog,
    event_slice: ContextEventSlice,
    named_slices: tuple[ContextEventSlice, ...] = (),
) -> ReplayedContextInput:
    """Load and revalidate every event-safe component referenced by an audit."""

    manifest = load_context_input_manifest(audit=audit, archive=archive)
    _validate_replayed_transcript_authority(
        manifest=manifest,
        archive=archive,
        event_log=event_log,
        event_slice=event_slice,
        named_slices=named_slices,
    )
    local_named_slices = tuple(
        item
        for item in named_slices
        if item.runtime_session_id == event_slice.runtime_session_id
    )
    authority_view: ContextEventSlice | ContextEventAuthorityView = (
        ContextEventAuthorityView(
            primary_slice=event_slice,
            named_slices=local_named_slices,
        )
        if local_named_slices
        else event_slice
    )
    (
        subagent_graph,
        subagent_graph_semantic_source,
        replay_acceleration,
        subagent_authority_events,
    ) = _restore_replay_subagent_graph(
        manifest=manifest,
        event_log=event_log,
        archive=archive,
    )
    try:
        build_input = collect_replay_context_inputs(
            input_manifest=manifest,
            event_slice=event_slice,
            named_slices=named_slices,
            subagent_graph=subagent_graph,
            subagent_graph_semantic_source=subagent_graph_semantic_source,
        )
    except ContextCandidateSelectionMismatch as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_candidate_selection_mismatch",
            "ledger-derived candidate selection differs from manifest",
        ) from exc
    except ContextEventSliceError as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_input_event_slice_untrusted",
            "context input event slice is not the audited authority range",
        ) from exc
    if build_context_snapshot(
        build_input,
        long_horizon_attribution=manifest.snapshot.long_horizon_attribution,
    ) != manifest.snapshot:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_snapshot_mismatch",
            "replayed context snapshot differs from manifest",
        )
    _validate_replayed_candidates(
        manifest=manifest,
        archive=archive,
        event_slice=authority_view,
        named_slices=named_slices,
        subagent_authority_events=subagent_authority_events,
    )
    summary_id = manifest.snapshot.authority_slice_plan.transcript_window.compaction_summary_artifact_id
    summary_text = None
    if summary_id is not None:
        try:
            summary_text = archive.get_text(
                summary_id,
                session_id=audit.source_runtime_session_id,
            )
        except Exception as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.ARTIFACT_MISSING,
                "context_compaction_summary_missing",
                "context compaction summary artifact is unavailable",
            ) from exc
    source_document = None
    source_id = (
        manifest.snapshot.authority_slice_plan.transcript_window
        .window_compaction_source_document_artifact_id
    )
    if source_id is not None:
        try:
            source_document = WindowCompactionSourceDocumentFact.model_validate(
                json.loads(
                    archive.get_text(
                        source_id,
                        session_id=audit.source_runtime_session_id,
                    )
                )
            )
        except Exception as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.ARTIFACT_MISSING,
                "context_window_compaction_source_missing",
                "window compaction source document artifact is unavailable",
            ) from exc
        expected_source_fingerprint = (
            manifest.snapshot.authority_slice_plan.transcript_window
            .window_compaction_source_document_fingerprint
        )
        if source_document.document_fingerprint != expected_source_fingerprint:
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "context_window_compaction_source_mismatch",
                "window compaction source document fingerprint differs from manifest",
            )
    try:
        authority_contracts = build_default_authority_materialization_contract_bundle()
        restored_transcript = restore_transcript_projection_from_base(
            event_log=event_log,
            archive=archive,
            runtime_session_id=audit.source_runtime_session_id,
            requested_through_sequence=audit.source_through_sequence,
            projection_base=manifest.transcript_authority.projection_base,
            event_domain_binding=authority_contracts.event_domain,
            materialization_contracts=(
                build_default_transcript_projection_materialization_contracts(
                    authority_contracts.limits
                )
            ),
            limits=authority_contracts.limits,
            deadline_monotonic=(
                monotonic() + authority_contracts.limits.operation_timeout_seconds
            ),
        )
        if (
            restored_transcript.projection_base
            != manifest.transcript_authority.projection_base
            or restored_transcript.semantic_source
            != manifest.transcript_authority.semantic_source
            or restored_transcript.domain_completeness_proof
            != manifest.transcript_authority.domain_completeness_proof
        ):
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "context_input_transcript_projection_base_mismatch",
                "manifest-owned transcript projection base cannot be restored exactly",
            )
        stable_entries = restored_transcript.state_store.stable_entries()
        terminal_content_texts = _hydrate_replay_terminal_content(
            runtime_session_id=audit.source_runtime_session_id,
            archive=archive,
            projection_window=(
                manifest.snapshot.authority_slice_plan.transcript_window
            ),
            stable_entries=stable_entries,
            documents=restored_transcript.document_registry,
        )
        compaction_terminal = _read_replay_compaction_terminal(
            event_log=event_log,
            terminal_ref=(
                manifest.snapshot.authority_slice_plan.transcript_window
                .compaction_terminal_ref
            ),
        )
        normalized = project_stable_context_transcript(
            runtime_session_id=audit.source_runtime_session_id,
            through_sequence=audit.source_through_sequence,
            current_user_anchor=manifest.snapshot.current_user_message.message_id,
            projection_window=(
                manifest.snapshot.authority_slice_plan.transcript_window
            ),
            stable_entries=stable_entries,
            documents=restored_transcript.document_registry,
            hydrated_message_contents=(
                restored_transcript.hydrated_message_contents
            ),
            terminal_content_text_by_artifact_id=terminal_content_texts,
            compaction_summary_text=summary_text,
            compaction_terminal_event=compaction_terminal,
            window_compaction_source_document=source_document,
        )
    except ContextInputReplayError:
        raise
    except (ContextEventSliceError, ValueError) as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_input_transcript_untrusted",
            "context input transcript cannot be restored from its durable projection",
        ) from exc
    if normalized.transcript.transcript_fingerprint != manifest.transcript_fingerprint:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_transcript_mismatch",
            "replayed transcript fingerprint mismatch",
        )
    units_fingerprint = context_fingerprint(
        "tool-result-units:v1",
        tuple(unit.unit_fingerprint for unit in normalized.tool_result_units),
    )
    if units_fingerprint != manifest.tool_result_units_fingerprint:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_tool_units_mismatch",
            "replayed tool-result units fingerprint mismatch",
        )
    prepared = prepare_tool_result_render_input(
        units=normalized.tool_result_units,
        transcript=normalized.transcript,
        policy_basis=manifest.snapshot.compile_policy.tool_result_basis,
        cache=None,
    )
    if (
        prepared.resolved_policy != manifest.tool_result_render_policy
        or prepared.render_input_fingerprint
        != manifest.tool_result_render_input_fingerprint
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_render_contract_mismatch",
            "replayed tool-result render input mismatch",
        )
    _validate_replayed_long_horizon_facts(
        manifest=manifest,
        event_slice=event_slice,
        named_slices=named_slices,
    )
    return ReplayedContextInput(
        manifest=manifest,
        snapshot_build_input=build_input,
        normalized_transcript=normalized,
        prepared_tool_results=prepared,
        prepared_candidates=manifest.prepared_candidate_set,
        subagent_graph_acceleration=replay_acceleration,
    )


def _hydrate_replay_terminal_content(
    *,
    runtime_session_id: str,
    archive,
    projection_window,
    stable_entries,
    documents,
) -> dict[str, str]:
    references = required_terminal_content_artifacts(
        stable_entries=stable_entries,
        projection_window=projection_window,
        documents=documents,
    )
    hydrated: dict[str, str] = {}
    for reference in references:
        try:
            text = archive.get_text(
                reference.artifact_id,
                session_id=runtime_session_id,
            )
        except Exception as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.ARTIFACT_MISSING,
                "context_terminal_projection_content_missing",
                "terminal projection content artifact is unavailable",
            ) from exc
        previous = hydrated.get(reference.artifact_id)
        if previous is not None and previous != text:
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "context_terminal_projection_content_conflict",
                "terminal projection content artifact identity is ambiguous",
            )
        hydrated[reference.artifact_id] = text
    return hydrated


def _read_replay_compaction_terminal(*, event_log: EventLog, terminal_ref):
    if terminal_ref is None:
        return None
    rows = event_log.read_raw_events_by_id((terminal_ref.event_id,))
    if len(rows) != 1:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_compaction_terminal_missing",
            "context compaction terminal event is unavailable",
        )
    raw = rows[0]
    if raw.sequence != terminal_ref.sequence:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_compaction_terminal_identity_mismatch",
            "context compaction terminal event identity differs from the manifest",
        )
    event = raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    if not isinstance(
        event,
        ContextCompactionCompletedEvent | ContextWindowCompactionCompletedEvent,
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_compaction_terminal_type_mismatch",
            "context compaction terminal event has the wrong schema",
        )
    return event


def _validate_replayed_long_horizon_facts(
    *,
    manifest: ContextCompileInputManifestFact,
    event_slice: ContextEventSlice,
    named_slices: tuple[ContextEventSlice, ...],
) -> None:
    local_named = tuple(
        item
        for item in named_slices
        if item.runtime_session_id == event_slice.runtime_session_id
    )
    if manifest.active_window.generation > 1:
        authority = ContextEventAuthorityView(
            primary_slice=event_slice,
            named_slices=local_named,
        )
        _validate_compacted_window_replay(
            manifest=manifest,
            authority=authority,
        )
        return
    try:
        decoded = tuple(
            frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            for frozen in event_slice.events
        )
        store = LongHorizonStateStore(
            decoded,
            initial_through_sequence=event_slice.from_sequence - 1,
        )
    except Exception as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "long_horizon_event_slice_untrusted",
            "long-horizon facts cannot be folded from the audited ledger slice",
        ) from exc
    snapshot = manifest.snapshot
    run_id = snapshot.identity.run_id
    chain = store.window_state(run_id)
    if chain is None or chain.active_window_id is None:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "long_horizon_active_window_missing",
            "manifest active context window cannot be reconstructed",
        )
    window = chain.windows[chain.active_window_id]
    projection = store.projection_state(window.window_id)
    rollout_owner = (
        snapshot.long_horizon_attribution.rollout_account_owner_runtime_session_id
    )
    if rollout_owner == event_slice.runtime_session_id:
        rollout = store.rollout_state(manifest.rollout_state.account_id)
    else:
        rollout_slices = tuple(
            item for item in named_slices if item.runtime_session_id == rollout_owner
        )
        if not rollout_slices:
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "long_horizon_rollout_authority_slice_missing",
                "manifest rollout account owner lacks audited named authority",
            )
        try:
            primary_rollout_slice = max(
                rollout_slices,
                key=lambda item: item.through_sequence,
            )
            rollout_authority = (
                primary_rollout_slice
                if len(rollout_slices) == 1
                else ContextEventAuthorityView(
                    primary_slice=primary_rollout_slice,
                    named_slices=tuple(
                        item
                        for item in rollout_slices
                        if item is not primary_rollout_slice
                    ),
                )
            )
            _account, rollout = fold_sparse_rollout_state(
                event_slice=rollout_authority,
                account_id=manifest.rollout_state.account_id,
                through_sequence=manifest.rollout_state.through_sequence,
            )
        except Exception as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.LEDGER_UNTRUSTED,
                "long_horizon_rollout_authority_slice_untrusted",
                "rollout facts cannot be folded from the audited named slice",
            ) from exc
    if (
        window != manifest.active_window
        or projection != manifest.projection_state
        or rollout != manifest.rollout_state
    ):
        mismatched = ",".join(
            name
            for name, differs in (
                ("window", window != manifest.active_window),
                ("projection", projection != manifest.projection_state),
                ("rollout", rollout != manifest.rollout_state),
            )
            if differs
        )
        if rollout is not None and rollout != manifest.rollout_state:
            actual_payload = rollout.model_dump(mode="json")
            expected_payload = manifest.rollout_state.model_dump(mode="json")
            rollout_fields = ",".join(
                key
                for key, value in actual_payload.items()
                if value != expected_payload.get(key)
            )
            mismatched += (
                f"[{rollout_fields};"
                f"actual_through={rollout.through_sequence};"
                f"expected_through={manifest.rollout_state.through_sequence}]"
            )
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "long_horizon_state_mismatch",
            f"replayed long-horizon state differs from manifest: {mismatched}",
        )
    rewrite_refs = tuple(
        frozen.to_reference(event_slice.runtime_session_id)
        for frozen in event_slice.events
        if frozen.event_type == EventType.CONTEXT_PROJECTION_REWRITE_PAGE
        and (
            event := frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        ).window_id
        == window.window_id
        and event.to_projection_generation <= projection.projection_generation
    )
    if (
        rewrite_refs
        != snapshot.long_horizon_attribution.projection_rewrite_event_refs
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "long_horizon_projection_rewrite_refs_mismatch",
            (
                "replayed projection rewrite references differ from manifest: "
                f"actual_sequences={tuple(item.sequence for item in rewrite_refs[:32])};"
                "expected_sequences="
                f"{tuple(item.sequence for item in snapshot.long_horizon_attribution.projection_rewrite_event_refs[:32])}"
            ),
        )


def _validate_compacted_window_replay(
    *,
    manifest: ContextCompileInputManifestFact,
    authority: ContextEventAuthorityView,
) -> None:
    decoded = tuple(
        frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        for frozen in authority.events
    )
    active_window = manifest.active_window
    opens = tuple(
        event
        for event in decoded
        if isinstance(event, ContextWindowOpenedEvent)
        and event.window.window_id == active_window.window_id
    )
    if len(opens) != 1 or opens[0].window != active_window:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "long_horizon_active_window_mismatch",
            "compacted active window differs from its durable opening",
        )
    if any(
        isinstance(event, ContextWindowClosedEvent)
        and event.window_id == active_window.window_id
        for event in decoded
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "long_horizon_active_window_closed",
            "manifest names a closed context window as active",
        )
    projection_events = (
        opens[0],
        *(
            event
            for event in decoded
            if isinstance(event, ContextProjectionRewritePageEvent)
            and event.window_id == active_window.window_id
        ),
    )
    try:
        projection_reducer = ContextWindowProjectionReducer()
        projection_reducer.apply_committed(projection_events)
        projection = projection_reducer.state(active_window.window_id)
        _account, rollout = fold_sparse_rollout_state(
            event_slice=authority,
            account_id=manifest.rollout_state.account_id,
            through_sequence=manifest.rollout_state.through_sequence,
        )
    except Exception as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "long_horizon_sparse_authority_untrusted",
            "compacted long-horizon facts cannot be reconstructed",
        ) from exc
    if projection != manifest.projection_state or rollout != manifest.rollout_state:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "long_horizon_state_mismatch",
            "compacted projection or rollout state differs from manifest",
        )
    rewrite_refs = tuple(
        frozen.to_reference(authority.runtime_session_id)
        for frozen in authority.events
        if frozen.event_type == EventType.CONTEXT_PROJECTION_REWRITE_PAGE
        and (
            event := frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        ).window_id
        == active_window.window_id
        and event.to_projection_generation
        <= manifest.projection_state.projection_generation
    )
    if (
        rewrite_refs
        != manifest.snapshot.long_horizon_attribution.projection_rewrite_event_refs
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "long_horizon_projection_rewrite_refs_mismatch",
            "compacted projection rewrite references differ from manifest",
        )


def _restore_replay_subagent_graph(
    *,
    manifest: ContextCompileInputManifestFact,
    event_log: EventLog,
    archive,
):
    from pulsara_agent.runtime.long_horizon.checkpoint import (
        SubagentGraphCheckpointContractMismatch,
        SubagentGraphCheckpointLedgerUntrusted,
        SubagentGraphCheckpointReadUnavailable,
        restore_subagent_graph_from_checkpoint,
    )
    from pulsara_agent.runtime.long_horizon.checkpoint_store import (
        EventLogSubagentGraphCheckpointReadPort,
    )
    from pulsara_agent.runtime.long_horizon.reducer_contract import (
        DEFAULT_SUBAGENT_GRAPH_REDUCER_REGISTRY,
        SubagentGraphReducerContractMismatch,
    )

    semantic = manifest.subagent_graph_semantic_source
    try:
        binding = DEFAULT_SUBAGENT_GRAPH_REDUCER_REGISTRY.resolve_binding(
            reducer_id=semantic.graph_reducer_id,
            reducer_version=semantic.graph_reducer_version,
            reducer_contract_fingerprint=(
                semantic.graph_reducer_contract_fingerprint
            ),
        )
    except Exception as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "subagent_graph_reducer_contract_unavailable",
            "historical subagent graph reducer contract is unavailable",
        ) from exc
    run_start_ref = manifest.snapshot.run_entry.run_start
    run_start_rows = event_log.read_raw_events_by_id((run_start_ref.event_id,))
    if len(run_start_rows) != 1:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_input_run_start_missing",
            "historical context RunStart is unavailable",
        )
    run_start_raw = run_start_rows[0]
    run_start = run_start_raw.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
    if (
        not isinstance(run_start, RunStartEvent)
        or run_start_raw.sequence != run_start_ref.sequence
        or run_start_raw.payload_fingerprint != run_start_ref.payload_fingerprint
        or run_start.subagent_graph_reducer_contract != binding.contract
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "subagent_graph_run_contract_mismatch",
            "historical RunStart reducer contract differs from replay binding",
        )
    from pulsara_agent.runtime.authority_materialization import (
        build_default_authority_materialization_contract_bundle,
    )

    authority_limits = (
        build_default_authority_materialization_contract_bundle().limits
    )
    policy = default_subagent_graph_checkpoint_policy(
        max_unreclaimable_ledger_events=(
            authority_limits.max_unreclaimable_ledger_events
        ),
        max_unreclaimable_charged_payload_bytes=(
            authority_limits.max_unreclaimable_charged_payload_bytes
        ),
    )
    read = EventLogSubagentGraphCheckpointReadPort(
        event_log=event_log,
        archive=archive,
        runtime_session_id=manifest.snapshot.identity.runtime_session_id,
    ).read_checkpoint_and_delta_snapshot(
        requested_through_sequence=(
            manifest.subagent_graph_acceleration.ledger_through_sequence
        ),
        reducer_contract=binding.contract,
        preferred_checkpoint_id=(
            manifest.subagent_graph_acceleration.checkpoint_id
        ),
        max_delta_events=policy.checkpoint_max_delta_events,
        max_delta_bytes=policy.checkpoint_max_delta_bytes,
        max_checkpoint_candidates=policy.rebase_max_checkpoint_candidates,
    )
    if isinstance(read, SubagentGraphCheckpointReadUnavailable):
        status = (
            ContextInputReplayStatus.CONTRACT_MISMATCH
            if read.reason_code == "reducer_contract_mismatch"
            else ContextInputReplayStatus.ARTIFACT_MISSING
        )
        raise ContextInputReplayError(
            status,
            f"subagent_checkpoint_{read.reason_code}",
            "subagent graph checkpoint acceleration is unavailable",
        )
    try:
        graph, replay_semantic, acceleration = (
            restore_subagent_graph_from_checkpoint(
                snapshot=read,
                reducer_binding=binding,
            )
        )
    except SubagentGraphCheckpointLedgerUntrusted as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "subagent_checkpoint_ledger_untrusted",
            "subagent graph checkpoint delta is not a trusted ledger prefix",
        ) from exc
    except SubagentGraphCheckpointContractMismatch as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "subagent_checkpoint_contract_mismatch",
            "subagent graph checkpoint differs from its durable contract",
        ) from exc
    except SubagentGraphReducerContractMismatch as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "subagent_graph_reducer_contract_mismatch",
            "historical graph event is unsupported by the frozen reducer contract",
        ) from exc
    if replay_semantic != semantic:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "subagent_graph_semantic_source_mismatch",
            "replayed subagent graph semantic source differs from manifest",
        )
    selection = manifest.snapshot.candidate_source_selections[0]
    results = tuple(graph.results[result_id] for result_id in selection.selected_source_ids)
    event_ids = tuple(result.provenance.terminal_event_id or "" for result in results)
    if any(not event_id for event_id in event_ids):
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "subagent_result_terminal_attribution_missing",
            "selected subagent result lacks terminal event attribution",
        )
    raw_events = event_log.read_raw_events_by_id(event_ids)
    if len(raw_events) != len(event_ids):
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "subagent_result_terminal_event_missing",
            "selected subagent result terminal event is unavailable",
        )
    frozen_events = tuple(
        FrozenStoredEvent.from_raw_envelope(raw) for raw in raw_events
    )
    for result, frozen in zip(results, frozen_events, strict=True):
        event = frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if (
            not isinstance(event, SubagentRunCompletedEvent)
            or event.result_id != result.result_id
            or event.subagent_run_id != result.subagent_run_id
            or event.summary != result.summary
            or event.result_artifact_id != result.final_message_artifact_id
            or tuple(event.artifact_ids) != result.artifact_ids
        ):
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "subagent_result_terminal_event_mismatch",
                "selected subagent result differs from restored graph",
            )
    return graph, replay_semantic, acceleration, frozen_events


def _validate_replayed_candidates(
    *,
    manifest: ContextCompileInputManifestFact,
    archive,
    event_slice: ContextEventSlice | ContextEventAuthorityView,
    named_slices: tuple[ContextEventSlice, ...],
    subagent_authority_events: tuple[FrozenStoredEvent, ...],
) -> None:
    snapshot = manifest.snapshot
    capability_matches = [
        (frozen, event)
        for frozen in event_slice.events
        if isinstance(
            (event := frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)),
            CapabilityExposureResolvedEvent,
        )
        and event.run_id == snapshot.identity.run_id
        and event.exposure == snapshot.capability_snapshot
    ]
    if len(capability_matches) != 1:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_candidate_capability_mismatch",
            "candidate replay lacks one exact capability exposure",
        )
    capability_frozen, _capability_event = capability_matches[0]
    selection = snapshot.candidate_source_selections[0]
    try:
        projections = collect_context_projection_references(
            event_slice=event_slice,
            capability_ref=capability_frozen.to_reference(
                event_slice.runtime_session_id
            ),
            capability=snapshot.capability_snapshot,
            explicit=(),
            run_id=snapshot.identity.run_id,
            projection_token_budget=(
                snapshot.compile_policy.candidate_collection.projection_token_budget
            ),
            subagent_result_ids=selection.selected_source_ids,
            subagent_authority_events=subagent_authority_events,
        )
        sources = ContextCandidateCollectionInput(
            system_prompt=_replay_static_instruction_text(
                snapshot=snapshot,
                source_id="base_system_instruction",
                archive=archive,
                required=True,
            ),
            memory_hook_prompt=_replay_static_instruction_text(
                snapshot=snapshot,
                source_id="memory_scope_instruction",
                archive=archive,
                required=False,
            ),
            capability_catalog=_replay_capability_projection_text(
                snapshot=snapshot,
                projection_kind="catalog",
                archive=archive,
            ),
            capability_active_skill=_replay_capability_projection_text(
                snapshot=snapshot,
                projection_kind="active_skill",
                archive=archive,
            ),
            plan_workflow=(
                PLAN_ACTIVE_INSTRUCTION if snapshot.plan_snapshot.active else None
            ),
        )
        run_starts = tuple(
            event
            for frozen in event_slice.events
            if frozen.run_id == snapshot.identity.run_id
            if isinstance(
                (event := frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)),
                RunStartEvent,
            )
        )
        if len(run_starts) != 1:
            raise ContextEventSliceError(
                "context replay requires one matching RunStart event"
            )
        run_start = run_starts[0]
        rollout_owner_runtime_session_id = (
            snapshot.long_horizon_attribution.rollout_account_owner_runtime_session_id
        )
        if rollout_owner_runtime_session_id == event_slice.runtime_session_id:
            rollout_event_slice = event_slice
        else:
            rollout_slices = tuple(
                item
                for item in named_slices
                if item.runtime_session_id == rollout_owner_runtime_session_id
            )
            if not rollout_slices:
                raise ContextEventSliceError(
                    "context replay requires frozen rollout-account authority"
                )
            primary_rollout_slice = max(
                rollout_slices,
                key=lambda item: item.through_sequence,
            )
            rollout_event_slice = (
                primary_rollout_slice
                if len(rollout_slices) == 1
                else ContextEventAuthorityView(
                    primary_slice=primary_rollout_slice,
                    named_slices=tuple(
                        item
                        for item in rollout_slices
                        if item is not primary_rollout_slice
                    ),
                )
            )
        openings = tuple(
            event.account
            for frozen in rollout_event_slice.events
            if isinstance(
                (event := frozen.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)),
                RolloutBudgetAccountOpenedEvent,
            )
            and event.account.account_id
            == snapshot.long_horizon_attribution.rollout_account_id
        )
        if len(openings) != 1:
            raise ContextEventSliceError(
                "context replay lacks one rollout account opening"
            )
        rollout_status_override = derive_rollout_status_candidate_from_state(
            event_slice=rollout_event_slice,
            account=openings[0],
            state=manifest.rollout_state,
            policy=run_start.long_horizon.rollout_status_hint_policy,
        )
        authorities = build_context_candidate_authorities(
            sources=sources,
            static_instructions=snapshot.static_instructions,
            projections=projections,
            capability_snapshot=snapshot.capability_snapshot,
            plan_snapshot=snapshot.plan_snapshot,
            event_slice=event_slice,
            rollout_event_slice=rollout_event_slice,
            rollout_account_id=(
                snapshot.long_horizon_attribution.rollout_account_id
            ),
            rollout_status_policy=(
                run_start.long_horizon.rollout_status_hint_policy
            ),
            rollout_status_override=rollout_status_override,
            derive_rollout_status_from_events=False,
            run_id=snapshot.identity.run_id,
            runtime_environment=snapshot.runtime_environment,
            compile_timing=snapshot.timing,
            source_selections=snapshot.candidate_source_selections,
            external_authority_events={
                event.event_id: event for event in subagent_authority_events
            },
        )
    except ContextInputReplayError:
        raise
    except ContextEventSliceError as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_input_candidate_ledger_untrusted",
            "candidate authority cannot be reconstructed from the ledger",
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_candidate_authority_invalid",
            "candidate authority cannot be reconstructed from durable sources",
        ) from exc
    if projections != snapshot.projections:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_candidate_projection_mismatch",
            "replayed candidate projections differ from manifest",
        )
    if authorities != snapshot.candidate_authorities:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_candidate_authority_mismatch",
            "replayed candidate authorities differ from manifest",
        )
    try:
        replayed = collect_context_candidates(snapshot=snapshot, cache=None).prepared
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_prepared_candidate_invalid",
            "prepared candidates cannot be reconstructed from durable authority",
        ) from exc
    manifest_candidates = manifest.prepared_candidate_set
    if (
        tuple(entry.candidate for entry in replayed.entries)
        != tuple(entry.candidate for entry in manifest_candidates.entries)
        or replayed.collection_decisions
        != manifest_candidates.collection_decisions
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_prepared_candidate_mismatch",
            "replayed prepared candidate facts differ from manifest",
        )


def _replay_static_instruction_text(
    *, snapshot, source_id: str, archive, required: bool
) -> str | None:
    fact = next(
        (item for item in snapshot.static_instructions if item.source_id == source_id),
        None,
    )
    if fact is None:
        if required:
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "context_input_static_instruction_missing",
                f"required static instruction is missing: {source_id}",
            )
        return None
    try:
        return archive.get_text(
            fact.content_artifact_id,
            session_id=snapshot.identity.runtime_session_id,
        )
    except Exception as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.ARTIFACT_MISSING,
            "context_input_static_instruction_artifact_missing",
            f"static instruction artifact is unavailable: {source_id}",
        ) from exc


def _replay_capability_projection_text(
    *, snapshot, projection_kind: str, archive
) -> str | None:
    projection = (
        snapshot.capability_snapshot.semantic.catalog_projection
        if projection_kind == "catalog"
        else snapshot.capability_snapshot.semantic.active_skill_projection
    )
    artifact_id = projection.rendered_prompt_artifact_id
    if artifact_id is None:
        return None
    try:
        return archive.get_text(
            artifact_id,
            session_id=snapshot.identity.runtime_session_id,
        )
    except Exception as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.ARTIFACT_MISSING,
            "context_input_capability_projection_artifact_missing",
            f"capability {projection_kind} projection artifact is unavailable",
        ) from exc


def replay_compiled_context(
    *,
    event: ContextCompiledEvent,
    archive,
    event_log: EventLog,
    event_slice: ContextEventSlice,
    named_slices: tuple[ContextEventSlice, ...] = (),
) -> ReplayedCompiledContext:
    """Rebuild and compare one complete provider-neutral compiled payload."""

    if event.status != "compiled" or event.input_audit is None:
        raise ContextInputReplayError(
            ContextInputReplayStatus.FACT_REPLAY_ONLY,
            "context_compiled_payload_not_available",
            "only a successful compiled context has an exact payload",
        )
    inputs = replay_context_input(
        audit=event.input_audit,
        archive=archive,
        event_log=event_log,
        event_slice=event_slice,
        named_slices=named_slices,
    )
    manifest = inputs.manifest
    if manifest.compiler_contract_version != "context-compiler-input:v2":
        raise ContextInputReplayError(
            ContextInputReplayStatus.FACT_REPLAY_ONLY,
            "context_compiler_contract_unavailable",
            "the historical compiler contract is unavailable in this process",
        )
    invocation = _rebind_replay_invocation(manifest=manifest, archive=archive)
    base_rendered = render_prepared_tool_result_units(
        prepared=inputs.prepared_tool_results,
        transcript=inputs.normalized_transcript.transcript,
        token_estimator=invocation.resolved_call.target.token_estimator,
    )
    rendered = apply_tool_observation_projection(
        units=inputs.normalized_transcript.tool_result_units,
        rendered=base_rendered,
        projection_state=manifest.projection_state,
        policy=inputs.prepared_tool_results.resolved_policy,
        token_estimator=invocation.resolved_call.target.token_estimator,
    )
    projected_refs = build_projected_tool_result_compile_refs(
        transcript=inputs.normalized_transcript.transcript,
        rendered_tool_results=rendered,
        projection_state=manifest.projection_state,
    )
    if projected_refs != manifest.projected_tool_result_refs:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_projected_tool_result_refs_mismatch",
            "replayed projected tool-result refs differ from manifest",
        )
    prepared_rollups = _replay_prepared_rollups(
        manifest=manifest,
        normalized=inputs.normalized_transcript,
        archive=archive,
        estimator=invocation.resolved_call.target.token_estimator,
    )
    compiled = compile_context_from_facts(
        facts=invocation,
        transcript=inputs.normalized_transcript.transcript,
        rendered_tool_results=rendered,
        prepared_rollups=prepared_rollups,
        section_candidates=inputs.prepared_candidates,
        transcript_provider_projection=(
            manifest.transcript_provider_projection
        ),
    )
    if (
        compiled.model_visible_named_fact_semantic_selection
        != manifest.transcript_authority.named_fact_selection.semantic_selection
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_named_fact_semantics_mismatch",
            "replayed named provider facts differ from manifest authority",
        )
    expected_payload_fp = provider_neutral_payload_fingerprint(compiled.llm_context)
    expected_decisions_fp = canonical_render_decisions_fingerprint(
        compiled.tool_result_render_decision_facts
    )
    measured = measure_long_horizon_context_budget(
        call=invocation.resolved_call,
        context=compiled.llm_context,
        estimate=compiled.final_token_estimate,
        window=manifest.active_window,
        projection_state=manifest.projection_state,
        policy=manifest.window_policy,
    )
    if (
        measured.decision != manifest.context_budget_decision
        or measured.pressure_shadow != manifest.projection_pressure_shadow
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "long_horizon_context_budget_decision_mismatch",
            "replayed long-horizon budget decision differs from manifest",
        )
    if (
        event.long_horizon_context_budget_decision
        != manifest.context_budget_decision
        or event.long_horizon_projection_pressure_shadow
        != manifest.projection_pressure_shadow
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "long_horizon_context_event_attribution_mismatch",
            "ContextCompiledEvent long-horizon facts differ from manifest",
        )
    event_shape = {
        "budget": event.budget,
        "sections": event.sections,
        "tool_specs": event.tool_specs,
        "diagnostics": event.diagnostics,
        "lifecycle_decisions": event.lifecycle_decisions,
        "canonical_decisions": event.tool_result_render_decision_facts,
        "provider_neutral_payload_fingerprint": (
            event.provider_neutral_payload_fingerprint
        ),
        "canonical_render_decisions_fingerprint": (
            event.canonical_render_decisions_fingerprint
        ),
    }
    replay_shape = {
        "budget": compiled.budget.to_event_value(),
        "sections": [item.to_event_value() for item in compiled.sections],
        "tool_specs": [item.to_event_value() for item in compiled.tool_specs],
        "diagnostics": [item.to_event_value() for item in compiled.diagnostics]
        + list(
            long_horizon_context_diagnostics(
                measurement=measured,
                target_unreachable=manifest.projection_target_unreachable,
            )
        ),
        "lifecycle_decisions": [dict(item) for item in compiled.lifecycle_decisions],
        "canonical_decisions": compiled.tool_result_render_decision_facts,
        "provider_neutral_payload_fingerprint": expected_payload_fp,
        "canonical_render_decisions_fingerprint": expected_decisions_fp,
    }
    if event_shape != replay_shape:
        mismatched_fields = tuple(
            key for key in event_shape if event_shape[key] != replay_shape[key]
        )
        diagnostic_detail = ""
        if "diagnostics" in mismatched_fields:
            diagnostic_detail = (
                ";event_diagnostics="
                f"{tuple(item.get('code') for item in event_shape['diagnostics'][:32])}"
                ";replay_diagnostics="
                f"{tuple(item.get('code') for item in replay_shape['diagnostics'][:32])}"
            )
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "compiled_context_payload_mismatch",
            (
                "replayed provider-neutral context differs from "
                f"ContextCompiledEvent: fields={mismatched_fields}"
                f"{diagnostic_detail}"
            ),
        )
    return ReplayedCompiledContext(inputs=inputs, compiled_context=compiled)


def _replay_prepared_rollups(
    *,
    manifest: ContextCompileInputManifestFact,
    normalized: NormalizedContextTranscript,
    archive,
    estimator,
):
    registry = default_observation_rollup_renderer_registry()
    units = {item.unit_id: item for item in normalized.tool_result_units}
    policy = manifest.window_policy
    replayed = []
    for prepared in manifest.prepared_rollup_units:
        try:
            source_units = tuple(
                units[unit_id] for unit_id in prepared.ordered_member_unit_ids
            )
        except KeyError as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "observation_rollup_member_missing",
                "manifest rollup member is absent from normalized tool results",
            ) from exc
        try:
            derived = prepare_observation_rollup_artifact(
                window_id=manifest.active_window.window_id,
                member_units=source_units,
                transcript=normalized.transcript,
                policy=policy,
                token_estimator=estimator,
                registry=registry,
            )
        except Exception as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "observation_rollup_contract_mismatch",
                "historical observation rollup cannot be rederived",
            ) from exc
        try:
            stored_text = archive.get_text(
                prepared.artifact_id,
                session_id=manifest.snapshot.identity.runtime_session_id,
            )
        except Exception as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.ARTIFACT_MISSING,
                "observation_rollup_artifact_missing",
                "historical observation rollup artifact is unavailable",
            ) from exc
        if (
            derived.fact != prepared.rollup
            or derived.anchor != prepared.compile_unit.placement_anchor
            or derived.rendered.text != stored_text
            or prepared.compile_unit.inline_text != stored_text
            or prepared.compile_unit.inline_content_sha256
            != derived.rendered.content_sha256
        ):
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "observation_rollup_payload_mismatch",
                "historical observation rollup differs from durable semantics",
            )
        replayed.append(prepared)
    return tuple(replayed)


def _rebind_replay_invocation(
    *,
    manifest: ContextCompileInputManifestFact,
    archive,
) -> ContextFactSnapshot:
    call_fact = manifest.snapshot.resolved_model_call
    target_fact = call_fact.target
    estimator = PulsaraHeuristicTokenEstimatorV1()
    if estimator.fact != target_fact.token_estimator:
        raise ContextInputReplayError(
            ContextInputReplayStatus.FACT_REPLAY_ONLY,
            "token_estimator_contract_unavailable",
            "the historical token estimator contract is unavailable",
        )
    profile = ProviderProfile(
        id=target_fact.provider_profile_id,
        wire_api=target_fact.api,
        supports_tools=target_fact.supports_tools,
        supports_reasoning=target_fact.supports_reasoning,
        model_identity_policy=ModelIdentityPolicy(target_fact.model_identity_policy),
    )
    target = ResolvedModelTarget(
        model_profile=ModelProfile(
            id=target_fact.model_id,
            role=ModelRole(target_fact.model_role),
            api=target_fact.api,
            provider=target_fact.provider,
            base_url=target_fact.endpoint_origin,
            provider_profile=profile,
            supports_tools=target_fact.supports_tools,
            supports_reasoning=target_fact.supports_reasoning,
        ),
        transport=_ReplayOnlyTransport(
            api=target_fact.api,
            binding_id=target_fact.transport_binding_id,
            contract_version=target_fact.transport_contract_version,
        ),
        effective_options=LLMOptions(
            reasoning_effort=target_fact.effective_options.reasoning_effort
        ),
        limits=target_fact.limits,
        context_budget=target_fact.context_budget,
        token_estimator=estimator,
        fact=target_fact,
    )
    materialized = tuple(
        _materialize_tool_spec(
            fact=fact,
            archive=archive,
            runtime_session_id=manifest.snapshot.identity.runtime_session_id,
        )
        for fact in manifest.snapshot.tool_specs
    )
    return ContextFactSnapshot(
        fact=manifest.snapshot,
        resolved_call=ResolvedModelCall(target=target, fact=call_fact),
        materialized_tool_specs=materialized,
    )


def _materialize_tool_spec(*, fact, archive, runtime_session_id: str):
    schema_fact = fact.input_schema
    if isinstance(schema_fact, ContextInlineToolSchemaFact):
        schema = schema_fact.schema_value
    elif isinstance(schema_fact, ContextArtifactToolSchemaFact):
        try:
            raw = archive.get_text(
                schema_fact.schema_artifact_id,
                session_id=runtime_session_id,
            )
        except Exception as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.ARTIFACT_MISSING,
                "tool_schema_artifact_missing",
                "a tool schema artifact is unavailable",
            ) from exc
        try:
            schema = freeze_json(json.loads(raw))
        except Exception as exc:
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "tool_schema_artifact_invalid",
                "a tool schema artifact is invalid",
            ) from exc
        if not isinstance(schema, FrozenJsonObjectFact):
            raise ContextInputReplayError(
                ContextInputReplayStatus.CONTRACT_MISMATCH,
                "tool_schema_artifact_not_object",
                "a tool schema artifact is not a JSON object",
            )
    else:  # pragma: no cover - closed union
        raise TypeError("unknown context tool schema fact")
    return ContextMaterializedToolSpecInput(
        fact=fact,
        materialized_schema=schema,
    )


__all__ = [
    "ContextInputReplayError",
    "ContextInputReplayStatus",
    "ReplayedContextInput",
    "ReplayedCompiledContext",
    "load_context_input_manifest",
    "replay_context_input",
    "replay_compiled_context",
]
