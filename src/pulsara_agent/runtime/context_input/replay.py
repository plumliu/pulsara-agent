"""Required provider replay and optional compiler-audit resolution.

Provider-visible payload authority is independent from optional compiler audit
artifacts.  This module deliberately has no decoder for the removed v8 flat
manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from time import monotonic

from pulsara_agent.event import (
    ContextCompiledEvent,
    ModelCallStartEvent,
    ProviderInputAppendCommittedEvent,
)
from pulsara_agent.event_log.historical_decoder import (
    decode_raw_stored_event_envelope,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.event_log.serialization import stable_event_identity
from pulsara_agent.llm.provider_input_materialization import (
    RecursivelyImmutableProviderInputCarrier,
    hydrate_carrier,
    message_semantic_fingerprint,
    tool_fragment_semantic_fingerprint,
)
from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.context_input_audit_storage import (
    ContextInputAuditComponentKind,
    ContextInputAuditMaterializationPlanFact,
    ContextInputAuditPageFact,
    ContextInputAuditRootFact,
)
from pulsara_agent.primitives.context_input_commit import (
    ContextCompileInputCommitFact,
)
from pulsara_agent.primitives.provider_input import (
    CommittedProviderInputReferenceFact,
    ProviderInputSemanticIdentityFact,
    ProviderInputReplayBindingIdentityFact,
    ProviderInputUnitMaterializationFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.context_source import LedgerAuthorityHorizonFact
from pulsara_agent.runtime.context_input.audit_materializer import (
    hydrate_context_input_audit_components,
)
from pulsara_agent.runtime.context_input.audit_storage import (
    ContextInputAuditArtifactIntegrityError,
    ContextInputAuditArtifactMissing,
    ContextInputAuditArtifactRepository,
    validate_context_input_audit_plan_reference,
)
from pulsara_agent.runtime.context_input.commit import (
    context_input_audit_component_ownership,
)
from pulsara_agent.runtime.context_input.event_slice import event_reference_from_stored
from pulsara_agent.runtime.provider_input.vector import (
    load_ledger_horizon_set,
    load_provider_input_vector,
    load_replay_binding_set,
)


_READ_DEADLINE_SECONDS = 30.0


class ContextInputReplayStatus(StrEnum):
    EXACT_AUDIT = "exact_audit"
    RECONSTRUCTED_AUDIT = "reconstructed_audit"
    AUDIT_UNAVAILABLE = "audit_unavailable"
    AUDIT_INTEGRITY_FAILURE = "audit_integrity_failure"


class ContextInputReplayError(RuntimeError):
    def __init__(self, status: ContextInputReplayStatus, reason_code: str) -> None:
        self.status = status
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class ExactCommittedProviderPayload:
    model_start: ModelCallStartEvent
    append: ProviderInputAppendCommittedEvent
    committed_reference: CommittedProviderInputReferenceFact
    units: tuple[ProviderInputUnitMaterializationFact, ...]
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...]
    replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...]
    carrier: RecursivelyImmutableProviderInputCarrier
    proof_fingerprint: str


@dataclass(frozen=True, slots=True)
class ExactAuditArtifact:
    status: ContextInputReplayStatus
    semantic_commit: ContextCompileInputCommitFact
    root: ContextInputAuditRootFact
    plan: ContextInputAuditMaterializationPlanFact
    pages: tuple[ContextInputAuditPageFact, ...]
    components: tuple[tuple[object, object], ...]
    proof_fingerprint: str


@dataclass(frozen=True, slots=True)
class ReconstructedAudit:
    status: ContextInputReplayStatus
    semantic_commit: ContextCompileInputCommitFact
    provider_payload: ExactCommittedProviderPayload
    reconstructed_component_kinds: tuple[str, ...]
    omitted_component_kinds: tuple[str, ...]
    artifact_diagnostic_code: str | None
    proof_fingerprint: str


@dataclass(frozen=True, slots=True)
class AuditUnavailable:
    status: ContextInputReplayStatus
    reason: str


@dataclass(frozen=True, slots=True)
class AuditIntegrityFailure:
    status: ContextInputReplayStatus
    reason: str


ContextInputAuditLoadOutcome = (
    ExactAuditArtifact | ReconstructedAudit | AuditUnavailable | AuditIntegrityFailure
)


def _decode_exact_events(event_log, event_ids: tuple[str, ...], *, deadline: float):
    rows = event_log.read_raw_events_by_id(
        event_ids,
        deadline_monotonic=deadline,
    )
    if tuple(row.event_id for row in rows) != event_ids:
        raise ContextInputReplayError(
            ContextInputReplayStatus.AUDIT_UNAVAILABLE,
            "required_event_missing",
        )
    return tuple(
        decode_raw_stored_event_envelope(row, DEFAULT_EVENT_SCHEMA_REGISTRY)
        for row in rows
    )


def load_committed_provider_payload_for_model_start(
    *,
    model_start_reference: ContextEventReferenceFact,
    event_log,
    provider_input_store,
    artifact_store,
    deadline_monotonic: float | None = None,
) -> ExactCommittedProviderPayload:
    """Rebuild exact provider-visible content without compiler audit artifacts."""

    del provider_input_store  # live cache is never authority for this proof
    deadline = (
        monotonic() + _READ_DEADLINE_SECONDS
        if deadline_monotonic is None
        else deadline_monotonic
    )
    (decoded_start,) = _decode_exact_events(
        event_log,
        (model_start_reference.event_id,),
        deadline=deadline,
    )
    if not isinstance(decoded_start, ModelCallStartEvent):
        raise ContextInputReplayError(
            ContextInputReplayStatus.AUDIT_INTEGRITY_FAILURE,
            "model_start_reference_type_mismatch",
        )
    if (
        event_reference_from_stored(
            decoded_start,
            runtime_session_id=model_start_reference.runtime_session_id,
        )
        != model_start_reference
        or decoded_start.provider_input_reference is None
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.AUDIT_INTEGRITY_FAILURE,
            "model_start_reference_identity_mismatch",
        )
    reference = decoded_start.provider_input_reference
    (decoded_append,) = _decode_exact_events(
        event_log,
        (reference.append_committed_event_identity.event_id,),
        deadline=deadline,
    )
    if not isinstance(decoded_append, ProviderInputAppendCommittedEvent):
        raise ContextInputReplayError(
            ContextInputReplayStatus.AUDIT_INTEGRITY_FAILURE,
            "provider_append_reference_type_mismatch",
        )
    if (
        stable_event_identity(
            decoded_append,
            runtime_session_id=model_start_reference.runtime_session_id,
        )
        != reference.append_committed_event_identity
        or decoded_append.resulting_core_state.unit_vector_root
        != reference.resulting_unit_vector_root
        or decoded_append.resulting_core_state.committed_authority_horizon_set
        != reference.authority_horizon_set
        or decoded_append.resulting_core_state.replay_binding_set
        != reference.replay_binding_set
        or decoded_append.generation_id != reference.generation_id
        or decoded_append.resulting_revision != reference.committed_generation_revision
        or decoded_append.resulting_core_state_fingerprint
        != reference.resulting_generation_core_state_fingerprint
        or decoded_append.semantic_commit_fingerprint
        != reference.semantic_commit_fingerprint
        or decoded_append.ordered_projection_identity_fingerprint
        != reference.ordered_projection_identity_fingerprint
    ):
        raise ContextInputReplayError(
            ContextInputReplayStatus.AUDIT_INTEGRITY_FAILURE,
            "provider_append_reference_identity_mismatch",
        )
    units, _reachable = load_provider_input_vector(
        archive=artifact_store,
        runtime_session_id=model_start_reference.runtime_session_id,
        root=reference.resulting_unit_vector_root,
        deadline_monotonic=deadline,
    )
    authority_horizons, _horizon_reachable = load_ledger_horizon_set(
        archive=artifact_store,
        runtime_session_id=model_start_reference.runtime_session_id,
        reference=reference.authority_horizon_set,
        deadline_monotonic=deadline,
    )
    replay_bindings, _binding_reachable = load_replay_binding_set(
        archive=artifact_store,
        runtime_session_id=model_start_reference.runtime_session_id,
        reference=reference.replay_binding_set,
        deadline_monotonic=deadline,
    )
    carrier = hydrate_carrier(units)
    return ExactCommittedProviderPayload(
        model_start=decoded_start,
        append=decoded_append,
        committed_reference=reference,
        units=units,
        authority_horizons=authority_horizons,
        replay_bindings=replay_bindings,
        carrier=carrier,
        proof_fingerprint=context_fingerprint(
            "exact-committed-provider-payload:v1",
            (
                model_start_reference,
                reference.reference_fingerprint,
                tuple(item.materialization_fingerprint for item in units),
                tuple(item.horizon_fingerprint for item in authority_horizons),
                tuple(item.identity_fingerprint for item in replay_bindings),
                carrier.carrier_fingerprint,
            ),
        ),
    )


def _matching_model_start_reference(
    *,
    event: ContextCompiledEvent,
    event_log,
    deadline: float,
) -> ContextEventReferenceFact | None:
    install = event.provider_input_preparation_install
    commit = event.semantic_commit
    if install is None or commit is None:
        return None
    # ModelStart is deliberately *not* one of the provider preparation's
    # companion events.  Its ID is frozen by the model lifecycle contract and
    # is therefore the only bounded lookup that can distinguish an abandoned
    # ContextCompiled preparation from a committed Start without scanning the
    # model-call stream.
    rows = event_log.read_raw_events_by_id(
        (f"model_call_start:{commit.resolved_model_call_id}",),
        deadline_monotonic=deadline,
    )
    for row in rows:
        candidate = decode_raw_stored_event_envelope(row, DEFAULT_EVENT_SCHEMA_REGISTRY)
        if (
            isinstance(candidate, ModelCallStartEvent)
            and candidate.resolved_call.resolved_model_call_id
            == event.resolved_call.resolved_model_call_id
            and candidate.context_id == event.context_id
            and candidate.model_call_index == event.model_call_index
            and candidate.provider_input_reference is not None
            and candidate.provider_input_reference.semantic_commit_fingerprint
            == commit.commit_fingerprint
            and candidate.provider_input_reference.provider_input_plan_fingerprint
            == commit.canonical_provider_input_plan_fingerprint
            and candidate.provider_input_reference.provider_input_plan_fingerprint
            == install.canonical_provider_input_plan_fingerprint
        ):
            return event_reference_from_stored(
                candidate,
                runtime_session_id=commit.runtime_session_id,
            )
    return None


def _validate_exact_audit_join(
    *,
    event: ContextCompiledEvent,
    root: ContextInputAuditRootFact,
    plan: ContextInputAuditMaterializationPlanFact,
    pages: tuple[ContextInputAuditPageFact, ...],
) -> None:
    commit = event.semantic_commit
    expectation = event.audit_expectation
    assert commit is not None and expectation is not None
    if (
        root.source_runtime_session_id != commit.runtime_session_id
        or root.source_run_id != commit.run_id
        or root.source_context_id != commit.context_id
        or root.source_resolved_model_call_id != commit.resolved_model_call_id
        or root.semantic_commit_fingerprint != commit.commit_fingerprint
        or root.materialization_key != expectation.materialization_key
        or root.materialization_contract_fingerprint
        != expectation.audit_contract_fingerprint
        or root.root_semantic_fingerprint
        != expectation.expected_root_semantic_fingerprint
    ):
        raise ContextInputAuditArtifactIntegrityError(
            "context input audit root semantic join mismatch"
        )
    if (
        plan.source_runtime_session_id != commit.runtime_session_id
        or plan.source_run_id != commit.run_id
        or plan.source_context_id != commit.context_id
        or plan.source_resolved_model_call_id != commit.resolved_model_call_id
        or plan.semantic_commit_fingerprint != commit.commit_fingerprint
        or plan.expectation_fingerprint != expectation.expectation_fingerprint
        or plan.materialization_key != expectation.materialization_key
        or plan.expected_root_artifact_id != expectation.expected_root_artifact_id
        or plan.expected_root_semantic_fingerprint
        != expectation.expected_root_semantic_fingerprint
        or plan.audit_contract_fingerprint != expectation.audit_contract_fingerprint
        or root.component_count != plan.component_count
        or root.page_count != plan.page_count
        or root.ordered_component_accumulator != plan.ordered_component_accumulator
        or root.ordered_page_accumulator != plan.ordered_page_accumulator
    ):
        raise ContextInputAuditArtifactIntegrityError(
            "context input audit plan/root join mismatch"
        )
    if tuple(page.page_ordinal for page in pages) != tuple(range(plan.page_count)):
        raise ContextInputAuditArtifactIntegrityError(
            "context input audit page ordinal set mismatch"
        )
    for page, reference in zip(pages, plan.page_references, strict=True):
        if (
            page.source_runtime_session_id != commit.runtime_session_id
            or page.source_run_id != commit.run_id
            or page.materialization_key != expectation.materialization_key
            or page.page_ordinal >= plan.page_count
            or reference.storage_fact_fingerprint != page.page_storage_fingerprint
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit page/plan join mismatch"
            )


def _validate_exact_reference_components(
    *,
    commit: ContextCompileInputCommitFact,
    provider: ExactCommittedProviderPayload,
    plan: ContextInputAuditMaterializationPlanFact,
    components: tuple[tuple[object, object], ...],
) -> None:
    """Bind audit references back to their canonical durable authorities."""

    for component in plan.components:
        if component.component_ownership is not context_input_audit_component_ownership(
            component.component_kind
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit component ownership registry mismatch"
            )
    by_kind = {kind: value for kind, value in components}

    expected_snapshot = {
        "snapshot_semantic_fingerprint": commit.snapshot_semantic_fingerprint,
        "source_reference_set_fingerprint": (
            commit.source_references.reference_set_fingerprint
        ),
        "source_through_sequence": commit.source_through_sequence,
    }
    if by_kind.get(ContextInputAuditComponentKind.SNAPSHOT) != expected_snapshot:
        raise ContextInputAuditArtifactIntegrityError(
            "context input audit snapshot reference mismatch"
        )

    expected_projection_identity = json.loads(
        canonical_json_bytes(commit.ordered_projection_identity)
    )
    if (
        by_kind.get(
            ContextInputAuditComponentKind.ORDERED_TRANSCRIPT_PROJECTION_IDENTITY
        )
        != expected_projection_identity
    ):
        raise ContextInputAuditArtifactIntegrityError(
            "context input audit ordered projection reference mismatch"
        )

    reference = provider.committed_reference
    expected_prepared_plan = {
        "plan_fingerprint": commit.prepared_provider_input_plan_fingerprint,
        "target_generation_id": reference.generation_id,
        "resulting_unit_vector_root_fingerprint": (
            reference.resulting_unit_vector_root.reference_fingerprint
        ),
        "ordered_projection_identity_fingerprint": (
            commit.ordered_projection_identity.identity_fingerprint
        ),
    }
    if (
        by_kind.get(ContextInputAuditComponentKind.PREPARED_PROVIDER_INPUT_PLAN)
        != expected_prepared_plan
    ):
        raise ContextInputAuditArtifactIntegrityError(
            "context input audit prepared provider-plan reference mismatch"
        )

    provider_identity = build_frozen_fact(
        ProviderInputSemanticIdentityFact,
        schema_version="provider_input_semantic_identity.v1",
        input_unit_count=reference.resulting_unit_vector_root.unit_count,
        ordered_unit_accumulator=(
            reference.resulting_unit_vector_root.ordered_unit_accumulator
        ),
        unit_vector_semantic_fingerprint=(
            reference.resulting_unit_vector_root.vector_semantic_fingerprint
        ),
        system_instruction_fingerprint=context_fingerprint(
            "provider-input-system-prompt:v1", provider.carrier.system_prompt
        ),
        tool_catalog_fingerprint=context_fingerprint(
            "provider-input-tool-catalog:v1",
            tuple(
                tool_fragment_semantic_fingerprint(item)
                for item in provider.carrier.ordered_tool_fragments
            ),
        ),
        provider_message_sequence_fingerprint=context_fingerprint(
            "provider-input-message-sequence:v1",
            tuple(
                message_semantic_fingerprint(item)
                for item in provider.carrier.ordered_messages
            ),
        ),
    )
    expected_canonical_plan = {
        "plan_fingerprint": commit.canonical_provider_input_plan_fingerprint,
        "generation_root_reference_fingerprint": (
            provider.append.resulting_core_state.root_reference.reference_fingerprint
        ),
        "unit_vector_root_reference_fingerprint": (
            reference.resulting_unit_vector_root.reference_fingerprint
        ),
        "authority_horizon_set_reference_fingerprint": (
            reference.authority_horizon_set.reference_fingerprint
        ),
        "replay_binding_set_reference_fingerprint": (
            reference.replay_binding_set.reference_fingerprint
        ),
        "provider_input_semantic_fingerprint": (provider_identity.semantic_fingerprint),
    }
    if (
        by_kind.get(ContextInputAuditComponentKind.CANONICAL_PROVIDER_INPUT_PLAN)
        != expected_canonical_plan
    ):
        raise ContextInputAuditArtifactIntegrityError(
            "context input audit canonical provider-plan reference mismatch"
        )


def load_context_input_audit(
    *,
    event: ContextCompiledEvent,
    event_log,
    provider_input_store,
    artifact_store,
    require_exact: bool = False,
    deadline_monotonic: float | None = None,
) -> ContextInputAuditLoadOutcome:
    """Resolve optional audit detail without changing canonical authority."""

    commit = event.semantic_commit
    expectation = event.audit_expectation
    if event.status != "compiled" or commit is None or expectation is None:
        return AuditUnavailable(
            ContextInputReplayStatus.AUDIT_UNAVAILABLE,
            "context_not_compiled",
        )
    deadline = (
        monotonic() + _READ_DEADLINE_SECONDS
        if deadline_monotonic is None
        else deadline_monotonic
    )
    start_reference = _matching_model_start_reference(
        event=event,
        event_log=event_log,
        deadline=deadline,
    )
    if start_reference is None:
        outcome: ContextInputAuditLoadOutcome = AuditUnavailable(
            ContextInputReplayStatus.AUDIT_UNAVAILABLE,
            "model_start_not_committed",
        )
        if require_exact:
            raise ContextInputReplayError(outcome.status, outcome.reason)
        return outcome

    provider: ExactCommittedProviderPayload | None = None
    provider_error: BaseException | None = None
    try:
        provider = load_committed_provider_payload_for_model_start(
            model_start_reference=start_reference,
            event_log=event_log,
            provider_input_store=provider_input_store,
            artifact_store=artifact_store,
            deadline_monotonic=deadline,
        )
    except BaseException as exc:
        provider_error = exc

    repository = ContextInputAuditArtifactRepository(artifact_store)
    artifact_error: str | None = None
    try:
        root, root_reference = repository.get_expected_root(
            artifact_id=expectation.expected_root_artifact_id,
            source_runtime_session_id=commit.runtime_session_id,
            source_run_id=commit.run_id,
            deadline_monotonic=deadline,
        )
        if (
            root.semantic_commit_fingerprint != commit.commit_fingerprint
            or root.materialization_key != expectation.materialization_key
            or root.root_semantic_fingerprint
            != expectation.expected_root_semantic_fingerprint
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "audit root semantic identity mismatch"
            )
        plan = repository.get_exact(
            reference=root.plan_artifact_reference,
            source_runtime_session_id=commit.runtime_session_id,
            source_run_id=commit.run_id,
            fact_type=ContextInputAuditMaterializationPlanFact,
            deadline_monotonic=deadline,
        )
        validate_context_input_audit_plan_reference(
            root=root,
            plan=plan,
            expected_plan_artifact_id=expectation.expected_plan_artifact_id,
        )
        pages = tuple(
            repository.get_exact(
                reference=reference,
                source_runtime_session_id=commit.runtime_session_id,
                source_run_id=commit.run_id,
                fact_type=ContextInputAuditPageFact,
                deadline_monotonic=deadline,
            )
            for reference in plan.page_references
        )
        _validate_exact_audit_join(
            event=event,
            root=root,
            plan=plan,
            pages=pages,
        )
        components = hydrate_context_input_audit_components(plan=plan, pages=pages)
        if provider is None:
            raise ContextInputAuditArtifactIntegrityError(
                "audit Start attribution cannot bind canonical provider authority"
            ) from provider_error
        _validate_exact_reference_components(
            commit=commit,
            provider=provider,
            plan=plan,
            components=components,
        )
        start_components = tuple(
            value
            for kind, value in components
            if kind is ContextInputAuditComponentKind.MODEL_START_ATTRIBUTION
        )
        expected_start_attribution = json.loads(
            canonical_json_bytes(
                (
                    start_reference,
                    event_reference_from_stored(
                        provider.append,
                        runtime_session_id=commit.runtime_session_id,
                    ),
                )
            )
        )
        if (
            len(start_components) != 1
            or start_components[0] != expected_start_attribution
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit ModelStart attribution mismatch"
            )
        return ExactAuditArtifact(
            ContextInputReplayStatus.EXACT_AUDIT,
            commit,
            root,
            plan,
            pages,
            components,
            context_fingerprint(
                "exact-context-input-audit:v1",
                (
                    root_reference.reference_fingerprint,
                    plan.plan_fingerprint,
                    tuple(page.page_storage_fingerprint for page in pages),
                ),
            ),
        )
    except (KeyError, FileNotFoundError, ContextInputAuditArtifactMissing):
        artifact_error = "audit_root_missing"
    except ContextInputAuditArtifactIntegrityError:
        artifact_error = "audit_artifact_integrity_failure"
    except Exception:
        # Storage availability, checkout deadline, or cancellation is not proof
        # that immutable audit bytes are corrupt.  Canonical provider replay may
        # still supply the reconstructable semantic view.
        artifact_error = "audit_artifact_unavailable"

    if provider is None:
        if artifact_error == "audit_artifact_integrity_failure":
            outcome = AuditIntegrityFailure(
                ContextInputReplayStatus.AUDIT_INTEGRITY_FAILURE,
                "audit_integrity_and_reconstruction_failure",
            )
        else:
            outcome = AuditUnavailable(
                ContextInputReplayStatus.AUDIT_UNAVAILABLE,
                "bounded_canonical_reconstruction_unavailable",
            )
    else:
        outcome = ReconstructedAudit(
            ContextInputReplayStatus.RECONSTRUCTED_AUDIT,
            commit,
            provider,
            (
                "semantic_commit",
                "provider_input_plan",
                "ordered_transcript_projection_identity",
            ),
            (
                "invocation_timing",
                "compiler_diagnostics",
                "render_operational_facts",
            ),
            artifact_error,
            context_fingerprint(
                "reconstructed-context-input-audit:v1",
                (commit.commit_fingerprint, provider.proof_fingerprint),
            ),
        )
    if require_exact and not isinstance(outcome, ExactAuditArtifact):
        raise ContextInputReplayError(
            outcome.status, getattr(outcome, "reason", "not_exact")
        )
    return outcome


__all__ = [
    "AuditIntegrityFailure",
    "AuditUnavailable",
    "ContextInputAuditLoadOutcome",
    "ContextInputReplayError",
    "ContextInputReplayStatus",
    "ExactAuditArtifact",
    "ExactCommittedProviderPayload",
    "ReconstructedAudit",
    "load_committed_provider_payload_for_model_start",
    "load_context_input_audit",
]
