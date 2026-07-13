"""Exact historical reconstruction of a durable context-input manifest."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json

from pulsara_agent.event import CapabilityExposureResolvedEvent, ContextCompiledEvent
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
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.tool_result import PreparedToolResultRenderInput
from pulsara_agent.runtime.context_input.event_slice import ContextEventSlice
from pulsara_agent.runtime.context_input.event_slice import ContextEventSliceError
from pulsara_agent.runtime.context_input.render import prepare_tool_result_render_input
from pulsara_agent.runtime.context_input.render import render_prepared_tool_result_units
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
    project_context_transcript,
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


def replay_context_input(
    *,
    audit: ContextCompileInputAuditFact,
    archive,
    event_slice: ContextEventSlice,
    named_slices: tuple[ContextEventSlice, ...] = (),
) -> ReplayedContextInput:
    """Load and revalidate every event-safe component referenced by an audit."""

    manifest = load_context_input_manifest(audit=audit, archive=archive)
    try:
        build_input = collect_replay_context_inputs(
            input_manifest=manifest,
            event_slice=event_slice,
            named_slices=named_slices,
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
    if build_context_snapshot(build_input) != manifest.snapshot:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "context_input_snapshot_mismatch",
            "replayed context snapshot differs from manifest",
        )
    _validate_replayed_candidates(
        manifest=manifest,
        archive=archive,
        event_slice=event_slice,
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
    try:
        normalized = project_context_transcript(
            snapshot=manifest.snapshot,
            event_slice=event_slice,
            compaction_summary_text=summary_text,
        )
    except (ContextEventSliceError, ValueError) as exc:
        raise ContextInputReplayError(
            ContextInputReplayStatus.LEDGER_UNTRUSTED,
            "context_input_transcript_untrusted",
            "context input transcript cannot be reconstructed from the ledger",
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
    return ReplayedContextInput(
        manifest=manifest,
        snapshot_build_input=build_input,
        normalized_transcript=normalized,
        prepared_tool_results=prepared,
        prepared_candidates=manifest.prepared_candidate_set,
    )


def _validate_replayed_candidates(
    *,
    manifest: ContextCompileInputManifestFact,
    archive,
    event_slice: ContextEventSlice,
) -> None:
    snapshot = manifest.snapshot
    capability_matches = [
        (frozen, event)
        for frozen in event_slice.events
        if isinstance(
            (event := frozen.decode_owned()),
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
        authorities = build_context_candidate_authorities(
            sources=sources,
            static_instructions=snapshot.static_instructions,
            projections=projections,
            capability_snapshot=snapshot.capability_snapshot,
            plan_snapshot=snapshot.plan_snapshot,
            event_slice=event_slice,
            run_id=snapshot.identity.run_id,
            runtime_environment=snapshot.runtime_environment,
            compile_timing=snapshot.timing,
            source_selections=snapshot.candidate_source_selections,
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
        event_slice=event_slice,
        named_slices=named_slices,
    )
    manifest = inputs.manifest
    if manifest.compiler_contract_version != "context-compiler-input:v1":
        raise ContextInputReplayError(
            ContextInputReplayStatus.FACT_REPLAY_ONLY,
            "context_compiler_contract_unavailable",
            "the historical compiler contract is unavailable in this process",
        )
    invocation = _rebind_replay_invocation(manifest=manifest, archive=archive)
    rendered = render_prepared_tool_result_units(
        prepared=inputs.prepared_tool_results,
        transcript=inputs.normalized_transcript.transcript,
        token_estimator=invocation.resolved_call.target.token_estimator,
    )
    compiled = compile_context_from_facts(
        facts=invocation,
        transcript=inputs.normalized_transcript.transcript,
        rendered_tool_results=rendered,
        section_candidates=inputs.prepared_candidates,
    )
    expected_payload_fp = provider_neutral_payload_fingerprint(compiled.llm_context)
    expected_decisions_fp = canonical_render_decisions_fingerprint(
        compiled.tool_result_render_decision_facts
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
        "diagnostics": [item.to_event_value() for item in compiled.diagnostics],
        "lifecycle_decisions": [dict(item) for item in compiled.lifecycle_decisions],
        "canonical_decisions": compiled.tool_result_render_decision_facts,
        "provider_neutral_payload_fingerprint": expected_payload_fp,
        "canonical_render_decisions_fingerprint": expected_decisions_fp,
    }
    if event_shape != replay_shape:
        raise ContextInputReplayError(
            ContextInputReplayStatus.CONTRACT_MISMATCH,
            "compiled_context_payload_mismatch",
            "replayed provider-neutral context differs from ContextCompiledEvent",
        )
    return ReplayedCompiledContext(inputs=inputs, compiled_context=compiled)


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
