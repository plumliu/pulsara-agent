"""Memory-owned post-compaction extension admission and request factory."""

from __future__ import annotations

from concurrent.futures import Executor
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import Callable, cast
from uuid import uuid4

from pulsara_agent.event.events import (
    AgentEvent,
    ContextCompactionCompletedEvent,
    ContextCompactionMemoryExtractionRequestedEvent,
    EventContext,
    EventType,
)
from pulsara_agent.event_log.serialization import (
    DEFAULT_EVENT_SCHEMA_REGISTRY,
    freeze_event_write_candidate,
)
from pulsara_agent.memory.compaction.contracts import (
    CompactionHumanEvidenceManifestConsumedFull,
)
from pulsara_agent.memory.compaction.evidence import (
    EXTRACTION_EVIDENCE_SELECTION_CONTRACT_FINGERPRINT,
    EXTRACTION_INPUT_CODEC_CONTRACT_FINGERPRINT,
)
from pulsara_agent.memory.compaction.manifest import (
    MANIFEST_ARTIFACT_CONTRACT_FINGERPRINT,
    ManifestPreparationOperation,
    build_human_evidence_manifest_plan,
    build_manifest_preparation_identity,
)
from pulsara_agent.memory.compaction.parser import PARSER_CONTRACT_FINGERPRINT
from pulsara_agent.memory.compaction.sanitizer import (
    SANITIZER_CONTRACT_FINGERPRINT,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.memory.scope import CTX_USER, MemoryDomainContext, workspace_scope
from pulsara_agent.ports.compaction_extensions import (
    CompactionPostCompletionExtensionPrivateHandleIdentity,
    PreparedCompactionPostCompletionExtensionAdmissionFailure,
    PreparedCompactionPostCompletionExtensionBatch,
    PreparedCompactionPostCompletionExtensionBatchIdentity,
    PreparedCompactionPostCompletionExtensionIntent,
    PreparedCompactionPostCompletionExtensionIntentIdentity,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.compaction import (
    CompactionMemoryExtractionContractFact,
    CompactionMemoryExtractionPolicyFact,
    CompactionPostCompletionExtensionAdmissionFailedFact,
    CompactionPostCompletionExtensionContractFact,
    CompactionPostCompletionExtensionLinkFact,
    CompactionPostCompletionExtensionRequestedFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.model_call import ResolvedModelTargetFact
from pulsara_agent.projection_jobs.compaction_memory_policy import (
    build_default_compaction_memory_extraction_policy,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_bounded_runtime_failure_diagnostic,
)


_EXTENSION_ID = "pulsara.compaction-memory-extraction"
_EXTENSION_VERSION = "1"
_NORMALIZATION_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-normalization-contract:v1",
    {
        "scope": "runtime-owned",
        "authority": "conversation-evidence",
        "verification": "inferred",
    },
)
_CANDIDATE_IDENTITY_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-candidate-identity-contract:v1",
    {
        "semantic": "kind/scope/normalized-statement",
        "occurrence": "job/request/ordinal/evidence/contract",
    },
)
_INPUT_DOCUMENT_SCHEMA_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-input-document-schema:v1",
    "compaction_memory_extraction_input_document.v1",
)
_OUTPUT_DOCUMENT_SCHEMA_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-output-document-schema:v1",
    "compaction_memory_extraction_output.v1",
)


def build_compaction_memory_extraction_contract() -> (
    CompactionMemoryExtractionContractFact
):
    return build_frozen_fact(
        CompactionMemoryExtractionContractFact,
        schema_version="compaction_memory_extraction_contract.v1",
        extractor_id="pulsara.compaction-memory-extraction",
        extractor_version="1",
        accepted_source_kind="direct_human_input_only",
        output_candidate_kinds=("Preference",),
        input_document_schema_fingerprint=_INPUT_DOCUMENT_SCHEMA_FINGERPRINT,
        output_document_schema_fingerprint=_OUTPUT_DOCUMENT_SCHEMA_FINGERPRINT,
        evidence_selection_contract_fingerprint=(
            EXTRACTION_EVIDENCE_SELECTION_CONTRACT_FINGERPRINT
        ),
        sanitizer_contract_fingerprint=SANITIZER_CONTRACT_FINGERPRINT,
        parser_contract_fingerprint=PARSER_CONTRACT_FINGERPRINT,
        normalization_contract_fingerprint=_NORMALIZATION_CONTRACT_FINGERPRINT,
        candidate_identity_contract_fingerprint=(
            _CANDIDATE_IDENTITY_CONTRACT_FINGERPRINT
        ),
        maximum_evidence_nodes=256,
        maximum_input_utf8_bytes=512 * 1024,
        maximum_output_utf8_bytes=64 * 1024,
        maximum_candidates=3,
        maximum_evidence_refs_per_candidate=8,
        maximum_statement_utf8_bytes=1000,
    )


def _request_event_id(
    *,
    runtime_session_id: str,
    compaction_id: str,
    completed_event_id: str,
    extension_contract_fingerprint: str,
) -> str:
    digest = context_fingerprint(
        "context-compaction-memory-extraction-request-id:v1",
        (
            runtime_session_id,
            compaction_id,
            completed_event_id,
            extension_contract_fingerprint,
        ),
    ).removeprefix("sha256:")
    return f"context_compaction_memory_extraction_requested:{digest}"


def _resolved_scope(domain: MemoryDomainContext) -> str:
    if domain.workspace_kind != "project":
        return CTX_USER
    assert domain.stable_project_key is not None
    return workspace_scope(domain.stable_project_key)


def _runtime_fact(cls, fingerprint_field: str, domain: str, **payload):
    payload[fingerprint_field] = context_fingerprint(domain, payload)
    return cls(**payload)


@dataclass(slots=True)
class _MemoryExtensionPrivateHandle:
    identity: CompactionPostCompletionExtensionPrivateHandleIdentity
    manifest_operation: ManifestPreparationOperation
    extension_contract: CompactionPostCompletionExtensionContractFact
    extraction_contract: CompactionMemoryExtractionContractFact
    extraction_policy: CompactionMemoryExtractionPolicyFact
    resolved_scope: str
    retire: Callable[[str], None] = field(repr=False)
    state: str = "active"
    prepared_batch_fingerprint: str | None = None
    stored_request_reference_fingerprint: str | None = None
    _lock: RLock = field(default_factory=RLock, repr=False)

    @property
    def active(self) -> bool:
        with self._lock:
            return self.state in {"active", "none_retained"}

    def bind_prepared_batch(self, fingerprint: str) -> None:
        with self._lock:
            if not self.active:
                raise RuntimeError("compaction extension handle is inactive")
            if self.prepared_batch_fingerprint not in {None, fingerprint}:
                raise RuntimeError("compaction extension batch identity drifted")
            self.prepared_batch_fingerprint = fingerprint

    def _require_batch(self, fingerprint: str) -> None:
        if self.prepared_batch_fingerprint != fingerprint:
            raise RuntimeError("compaction extension confirmation batch mismatch")

    def confirm_request_batch_full(
        self, *, prepared_batch_fingerprint: str, stored_request_reference
    ) -> None:
        stored_reference_fingerprint = context_fingerprint(
            "compaction-memory-extraction-stored-request-reference:v1",
            stored_request_reference.model_dump(mode="json"),
        )
        with self._lock:
            self._require_batch(prepared_batch_fingerprint)
            if self.state == "full_confirmed":
                if (
                    self.stored_request_reference_fingerprint
                    != stored_reference_fingerprint
                ):
                    raise RuntimeError(
                        "compaction extension FULL confirmation reference drifted"
                    )
                return
            if self.state not in {
                "active",
                "none_retained",
                "reconciliation_required",
            }:
                raise RuntimeError("compaction extension handle cannot confirm FULL")
            self.stored_request_reference_fingerprint = stored_reference_fingerprint
            self.state = "full_confirmed"
        self.retire(self.identity.handle_id)

    def retain_request_batch_none(self, *, prepared_batch_fingerprint: str) -> None:
        with self._lock:
            self._require_batch(prepared_batch_fingerprint)
            if self.state not in {"active", "none_retained"}:
                raise RuntimeError("compaction extension handle cannot retain NONE")
            self.state = "none_retained"

    def mark_request_batch_reconciliation_required(
        self, *, prepared_batch_fingerprint: str
    ) -> None:
        with self._lock:
            self._require_batch(prepared_batch_fingerprint)
            self.state = "reconciliation_required"

    def abandon_before_write(self, *, reason: str) -> None:
        del reason
        with self._lock:
            if self.prepared_batch_fingerprint is not None:
                raise RuntimeError("prepared extension batch cannot be abandoned")
            if self.state == "abandoned":
                return
            if self.state != "active":
                raise RuntimeError("compaction extension handle cannot be abandoned")
            self.state = "abandoned"
        self.manifest_operation.request_physical_cancel()
        if self.manifest_operation.snapshot_nowait().physical_state == "exited":
            self.retire(self.identity.handle_id)


@dataclass(slots=True)
class MemoryCompactionPostCompletionExtension:
    """Concrete memory extension hidden behind the generic compaction port."""

    archive: ArtifactStore
    runtime_session_id: str
    memory_domain: MemoryDomainContext
    resolved_model_target_factory: Callable[[], ResolvedModelTargetFact]
    physical_executor: Executor
    _accepting: bool = field(default=True, init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _handles: dict[str, _MemoryExtensionPrivateHandle] = field(
        default_factory=dict, init=False, repr=False
    )
    _manifest_operations: dict[str, ManifestPreparationOperation] = field(
        default_factory=dict, init=False, repr=False
    )

    @property
    def pending_physical_operation_count(self) -> int:
        return sum(
            item.snapshot_nowait().physical_state != "exited"
            for item in self._manifest_operations.values()
        )

    def _retire_handle(self, handle_id: str) -> None:
        handle = self._handles.pop(handle_id, None)
        if handle is not None:
            self._manifest_operations.pop(
                handle.manifest_operation.identity.preparation_id,
                None,
            )

    def _on_manifest_operation_exit(self, preparation_id: str) -> None:
        handle = next(
            (
                item
                for item in self._handles.values()
                if item.manifest_operation.identity.preparation_id == preparation_id
            ),
            None,
        )
        if handle is None:
            self._manifest_operations.pop(preparation_id, None)
            return
        if handle.state == "abandoned":
            self._retire_handle(handle.identity.handle_id)

    def _policy(self) -> CompactionMemoryExtractionPolicyFact:
        return build_default_compaction_memory_extraction_policy(
            model_target=self.resolved_model_target_factory(),
        )

    def _extension_contract(self) -> CompactionPostCompletionExtensionContractFact:
        request_contract = DEFAULT_EVENT_SCHEMA_REGISTRY.latest_contract_for_type(
            str(EventType.CONTEXT_COMPACTION_MEMORY_EXTRACTION_REQUESTED)
        )
        extraction = build_compaction_memory_extraction_contract()
        return build_frozen_fact(
            CompactionPostCompletionExtensionContractFact,
            schema_version="compaction_post_completion_extension_contract.v1",
            extension_id=_EXTENSION_ID,
            extension_version=_EXTENSION_VERSION,
            request_event_type=str(
                EventType.CONTEXT_COMPACTION_MEMORY_EXTRACTION_REQUESTED
            ),
            request_event_schema_fingerprint=(
                request_contract.event_schema_fingerprint
            ),
            source_manifest_contract_fingerprint=(
                MANIFEST_ARTIFACT_CONTRACT_FINGERPRINT
            ),
            admission_policy_fingerprint=context_fingerprint(
                "compaction-memory-extraction-admission-policy:v1",
                {
                    "extraction_contract": extraction.contract_fingerprint,
                    "input_codec": EXTRACTION_INPUT_CODEC_CONTRACT_FINGERPRINT,
                },
            ),
        )

    def _failure(self, *, stage: str, error: BaseException):
        contract = self._extension_contract()
        diagnostic = build_bounded_runtime_failure_diagnostic(
            error=error,
            redaction_profile_id="durable_projection_job_error.v1",
        )
        return _runtime_fact(
            PreparedCompactionPostCompletionExtensionAdmissionFailure,
            "preparation_fingerprint",
            "prepared-compaction-post-completion-extension-admission-failure:v1",
            extension_contract_fingerprint=contract.contract_fingerprint,
            failure_stage=stage,
            diagnostic=diagnostic,
        )

    def prepare_intent(
        self,
        *,
        runtime_session_id: str,
        event_context: EventContext,
        compaction_id: str,
        completed_event_id: str,
        trigger: str,
        phase: str | None,
        previous_keep_after_sequence: int,
        current_keep_after_sequence: int,
        current_through_sequence: int,
        predecessor_completed_event_id: str | None,
        transcript_authority_snapshot: object,
        event_lookup: Callable[[str], AgentEvent | None],
    ):
        if not self._accepting:
            return self._failure(
                stage="intent_factory",
                error=RuntimeError("compaction memory extension admission is closed"),
            )
        if runtime_session_id != self.runtime_session_id:
            return self._failure(
                stage="intent_factory",
                error=RuntimeError("compaction memory extension session mismatch"),
            )
        normalized_phase = phase or ("manual" if trigger == "manual" else "pre_run")
        try:
            policy = self._policy()
        except BaseException as exc:
            return self._failure(stage="target_resolution", error=exc)
        if (
            not policy.enabled
            or trigger not in policy.allowed_triggers
            or normalized_phase not in policy.allowed_phases
        ):
            return None
        try:
            plan = build_human_evidence_manifest_plan(
                runtime_session_id=runtime_session_id,
                authority_snapshot=transcript_authority_snapshot,
                previous_keep_after_sequence=previous_keep_after_sequence,
                current_keep_after_sequence=current_keep_after_sequence,
                current_through_sequence=current_through_sequence,
                predecessor_completed_event_id=predecessor_completed_event_id,
                event_lookup=event_lookup,
            )
            self._generation += 1
            preparation_id = f"compaction_manifest:{compaction_id}:{self._generation}"
            operation = ManifestPreparationOperation(
                identity=build_manifest_preparation_identity(
                    preparation_id=preparation_id,
                    generation=self._generation,
                    plan=plan,
                    deadline_monotonic=monotonic() + 30.0,
                ),
                plan=plan,
                archive=self.archive,
                runtime_session_id=runtime_session_id,
                physical_executor=self.physical_executor,
            )
            self._manifest_operations[preparation_id] = operation
        except BaseException as exc:
            return self._failure(stage="manifest_prepare", error=exc)
        try:
            contract = self._extension_contract()
            extraction = build_compaction_memory_extraction_contract()
            scope = _resolved_scope(self.memory_domain)
        except BaseException as exc:
            self._manifest_operations.pop(preparation_id, None)
            return self._failure(stage="intent_factory", error=exc)
        try:
            intent = self._prepare_intent_with_frozen_authority(
                runtime_session_id=runtime_session_id,
                compaction_id=compaction_id,
                completed_event_id=completed_event_id,
                human_evidence_manifest_preparation=operation,
                policy=policy,
                contract=contract,
                extraction=extraction,
                scope=scope,
            )
            operation.add_exit_callback(
                lambda: self._on_manifest_operation_exit(preparation_id)
            )
            operation.start()
            return intent
        except BaseException as exc:
            self._retire_handle(
                next(
                    (
                        item.identity.handle_id
                        for item in self._handles.values()
                        if item.manifest_operation is operation
                    ),
                    "",
                )
            )
            self._manifest_operations.pop(preparation_id, None)
            return self._failure(stage="intent_factory", error=exc)

    def _prepare_intent_with_frozen_authority(
        self,
        *,
        runtime_session_id: str,
        compaction_id: str,
        completed_event_id: str,
        human_evidence_manifest_preparation,
        policy: CompactionMemoryExtractionPolicyFact,
        contract: CompactionPostCompletionExtensionContractFact,
        extraction: CompactionMemoryExtractionContractFact,
        scope: str,
    ):
        completed_id = completed_event_id
        request_id = _request_event_id(
            runtime_session_id=runtime_session_id,
            compaction_id=compaction_id,
            completed_event_id=completed_id,
            extension_contract_fingerprint=contract.contract_fingerprint,
        )
        link = build_frozen_fact(
            CompactionPostCompletionExtensionLinkFact,
            schema_version="compaction_post_completion_extension_link.v1",
            compaction_id=compaction_id,
            completed_event_id=completed_id,
            request_event_id=request_id,
            extension_contract_fingerprint=contract.contract_fingerprint,
        )
        handle_identity = _runtime_fact(
            CompactionPostCompletionExtensionPrivateHandleIdentity,
            "identity_fingerprint",
            "compaction-post-completion-extension-private-handle-identity:v1",
            extension_id=_EXTENSION_ID,
            handle_id=f"compaction_extension:{uuid4().hex}",
            generation=self._generation,
            manifest_preparation_identity_fingerprint=(
                human_evidence_manifest_preparation.identity.identity_fingerprint
            ),
        )
        handle = _MemoryExtensionPrivateHandle(
            identity=handle_identity,
            manifest_operation=human_evidence_manifest_preparation,
            extension_contract=contract,
            extraction_contract=extraction,
            extraction_policy=policy,
            resolved_scope=scope,
            retire=self._retire_handle,
        )
        self._handles[handle_identity.handle_id] = handle
        business_occurrence = context_fingerprint(
            "compaction-memory-extraction-request-occurrence:v1",
            (runtime_session_id, compaction_id, completed_id, request_id),
        )
        identity = _runtime_fact(
            PreparedCompactionPostCompletionExtensionIntentIdentity,
            "intent_fingerprint",
            "prepared-compaction-post-completion-extension-intent:v1",
            extension_contract_fingerprint=contract.contract_fingerprint,
            completed_event_id=completed_id,
            request_event_id=request_id,
            extension_link_id=link.extension_link_id,
            business_occurrence_fingerprint=business_occurrence,
            private_handle_identity_fingerprint=handle_identity.identity_fingerprint,
        )
        return PreparedCompactionPostCompletionExtensionIntent(
            identity=identity,
            extension_contract=contract,
            private_handle=handle,
        )

    def prepare_completion_disposition(
        self, *, preparation, completed_event: ContextCompactionCompletedEvent
    ):
        if isinstance(
            preparation, PreparedCompactionPostCompletionExtensionAdmissionFailure
        ):
            return build_frozen_fact(
                CompactionPostCompletionExtensionAdmissionFailedFact,
                schema_version=(
                    "compaction_post_completion_extension_admission_failed.v1"
                ),
                disposition_kind="admission_failed",
                extension_contract_fingerprint=(
                    preparation.extension_contract_fingerprint
                ),
                failure_stage=preparation.failure_stage,
                diagnostic=preparation.diagnostic,
            )
        if preparation.identity.completed_event_id != completed_event.id:
            raise ValueError("extension intent/completed event identity mismatch")
        handle = cast(_MemoryExtensionPrivateHandle, preparation.private_handle)
        consumed = handle.manifest_operation.consume_full_or_abandon()
        if not isinstance(consumed, CompactionHumanEvidenceManifestConsumedFull):
            handle.abandon_before_write(reason=consumed.failure_stage)
            return build_frozen_fact(
                CompactionPostCompletionExtensionAdmissionFailedFact,
                schema_version=(
                    "compaction_post_completion_extension_admission_failed.v1"
                ),
                disposition_kind="admission_failed",
                extension_contract_fingerprint=(
                    preparation.extension_contract.contract_fingerprint
                ),
                failure_stage=consumed.failure_stage,
                diagnostic=consumed.diagnostic,
            )
        policy = handle.extraction_policy
        extraction = handle.extraction_contract
        scope = handle.resolved_scope
        event_semantic = context_fingerprint(
            "context-compaction-memory-extraction-request-semantic:v1",
            {
                "manifest_semantic": consumed.manifest_reference.manifest_semantic_fingerprint,
                "memory_domain_id": self.memory_domain.memory_domain_id,
                "resolved_scope": scope,
                "extraction_contract": extraction.contract_fingerprint,
            },
        )
        request = ContextCompactionMemoryExtractionRequestedEvent(
            id=preparation.identity.request_event_id,
            **EventContext(
                run_id=completed_event.run_id,
                turn_id=completed_event.turn_id,
                reply_id=completed_event.reply_id,
            ).event_fields(),
            extension_link=build_frozen_fact(
                CompactionPostCompletionExtensionLinkFact,
                schema_version="compaction_post_completion_extension_link.v1",
                compaction_id=completed_event.compaction_id,
                completed_event_id=completed_event.id,
                request_event_id=preparation.identity.request_event_id,
                extension_contract_fingerprint=(
                    preparation.extension_contract.contract_fingerprint
                ),
            ),
            human_evidence_manifest_reference=consumed.manifest_reference,
            memory_domain_id=self.memory_domain.memory_domain_id,
            resolved_scope=scope,
            extraction_contract=extraction,
            extraction_policy=policy,
            business_occurrence_fingerprint=(
                preparation.identity.business_occurrence_fingerprint
            ),
            event_semantic_fingerprint=event_semantic,
        )
        candidate = freeze_event_write_candidate(request)
        batch_identity = _runtime_fact(
            PreparedCompactionPostCompletionExtensionBatchIdentity,
            "prepared_batch_fingerprint",
            "prepared-compaction-post-completion-extension-batch:v1",
            extension_contract_fingerprint=(
                preparation.extension_contract.contract_fingerprint
            ),
            extension_link_id=request.extension_link.extension_link_id,
            request_event_id=request.id,
            request_event_type=str(request.type),
            request_event_schema_fingerprint=candidate.event_schema_fingerprint,
            request_event_payload_fingerprint=candidate.payload_fingerprint,
        )
        handle.bind_prepared_batch(batch_identity.prepared_batch_fingerprint)
        return PreparedCompactionPostCompletionExtensionBatch(
            identity=batch_identity,
            extension_link=request.extension_link,
            request_event_candidate=candidate,
        )

    async def stop_admission_and_drain(self, *, deadline_monotonic: float) -> None:
        self._accepting = False
        handles = tuple(self._handles.values())
        operations = tuple(self._manifest_operations.values())
        for handle in handles:
            if handle.active and handle.prepared_batch_fingerprint is None:
                handle.abandon_before_write(reason="session_close")
        for operation in operations:
            operation.request_physical_cancel()
        for operation in operations:
            if not await operation.wait_physical_exit(
                deadline_monotonic=deadline_monotonic
            ):
                raise TimeoutError(
                    "compaction manifest physical preparation did not exit"
                )
        self._handles.clear()
        self._manifest_operations.clear()


def requested_disposition(
    batch: PreparedCompactionPostCompletionExtensionBatch,
) -> CompactionPostCompletionExtensionRequestedFact:
    return build_frozen_fact(
        CompactionPostCompletionExtensionRequestedFact,
        schema_version="compaction_post_completion_extension_requested.v1",
        disposition_kind="requested",
        extension_link=batch.extension_link,
    )


__all__ = [
    "MemoryCompactionPostCompletionExtension",
    "build_compaction_memory_extraction_contract",
    "requested_disposition",
]
