"""Transcript-derived human evidence manifest and physical preparation owner."""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from threading import RLock
from time import monotonic
from typing import Any, Callable, Protocol

from pulsara_agent.event.events import AgentEvent, RunStartEvent
from pulsara_agent.memory.compaction.contracts import (
    CompactionHumanEvidenceManifestConsumedAbandoned,
    CompactionHumanEvidenceManifestConsumedFull,
    CompactionHumanEvidenceManifestConsumptionOutcome,
    CompactionHumanEvidenceManifestPreparationFailureSnapshot,
    CompactionHumanEvidenceManifestPreparationHandle,
    CompactionHumanEvidenceManifestPreparationIdentity,
    CompactionHumanEvidenceManifestPreparationSnapshot,
)
from pulsara_agent.memory.compaction.sanitizer import (
    SANITIZER_CONTRACT_FINGERPRINT,
    sanitize_compaction_evidence,
)
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives._context_base import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.compaction import (
    CompactionHumanEvidenceArtifactSelectionProjectionFact,
    CompactionHumanEvidenceInlineSelectionProjectionFact,
    CompactionHumanEvidenceLeafAttributionFact,
    CompactionHumanEvidenceLeafSemanticFact,
    CompactionHumanEvidenceManifestAttributionFact,
    CompactionHumanEvidenceManifestPageFact,
    CompactionHumanEvidenceManifestReferenceFact,
    CompactionHumanEvidenceManifestRootFact,
    CompactionHumanEvidenceManifestSemanticFact,
    CompactionHumanEvidenceSelectionProjectionFact,
    CompactionHumanEvidenceSelectionWindowAttributionFact,
    ContentAddressedArtifactReferenceFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_bounded_runtime_failure_diagnostic,
)
from pulsara_agent.primitives.transcript_projection import (
    InlineNormalizedMessageContentFact,
    TranscriptMessageLeafEntryFact,
    TranscriptProjectionLeafEntryReferenceFact,
)


class GovernanceTranscriptAuthoritySnapshotLike(Protocol):
    reducer_evidence_snapshot: Any
    ledger_through_sequence: int
    ledger_continuity_accumulator: str


MANIFEST_SOURCE_SELECTION_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-human-evidence-source-selection-contract:v1",
    {
        "eligible": "message:user:user:current_user:host_user_input",
        "window": "previous-keep-exclusive/current-keep-inclusive",
        "ordering": "transcript-causal",
    },
)
MANIFEST_SELECTION_PROJECTION_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-human-evidence-selection-projection-contract:v1",
    {
        "sanitizer": SANITIZER_CONTRACT_FINGERPRINT,
        "inline_max_utf8_bytes": 8 * 1024,
        "oversize": "artifact-full-permanently-omitted",
    },
)
MANIFEST_ARTIFACT_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-human-evidence-manifest-artifact-contract:v1",
    {"page_leaves": 256, "page_bytes": 1024 * 1024, "root": "content-addressed"},
)


@dataclass(frozen=True, slots=True)
class PreparedManifestArtifact:
    reference: ContentAddressedArtifactReferenceFact
    content: str
    semantic_metadata: FrozenJsonObjectFact


@dataclass(frozen=True, slots=True)
class CompactionHumanEvidenceManifestPlan:
    semantic: CompactionHumanEvidenceManifestSemanticFact
    attribution: CompactionHumanEvidenceManifestAttributionFact
    reference: CompactionHumanEvidenceManifestReferenceFact
    root: CompactionHumanEvidenceManifestRootFact
    pages: tuple[CompactionHumanEvidenceManifestPageFact, ...]
    artifacts: tuple[PreparedManifestArtifact, ...]
    plan_fingerprint: str


def _accumulate(domain: str, values: tuple[str, ...]) -> str:
    result = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        result = context_fingerprint(f"{domain}:step", (result, value))
    return result


def _artifact_reference(
    *, artifact_kind: str, media_type: str, content: str
) -> ContentAddressedArtifactReferenceFact:
    encoded = content.encode("utf-8")
    digest = sha256(encoded).hexdigest()
    artifact_id = f"{artifact_kind}:{digest}"
    return build_frozen_fact(
        ContentAddressedArtifactReferenceFact,
        schema_version="content_addressed_artifact_reference.v1",
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        media_type=media_type,
        content_sha256=digest,
        content_bytes=len(encoded),
        artifact_contract_fingerprint=MANIFEST_ARTIFACT_CONTRACT_FINGERPRINT,
    )


def _leaf_reference(entry: TranscriptMessageLeafEntryFact, runtime_session_id: str):
    return build_frozen_fact(
        TranscriptProjectionLeafEntryReferenceFact,
        schema_version="transcript_projection_leaf_entry_reference.v2",
        runtime_session_id=runtime_session_id,
        entry_kind="message",
        ordinal=entry.ordinal.value,
        entry_semantic_fingerprint=entry.semantic_identity.semantic_fingerprint,
        entry_fact_fingerprint=entry.fact_fingerprint,
        source_event_references=entry.source_event_refs,
    )


def _eligible_human_leaf(
    *,
    entry: object,
    previous_keep_after_sequence: int,
    current_keep_after_sequence: int,
    event_lookup: Callable[[str], AgentEvent | None],
    runtime_session_id: str,
) -> tuple[
    CompactionHumanEvidenceLeafSemanticFact,
    CompactionHumanEvidenceLeafAttributionFact,
    CompactionHumanEvidenceSelectionProjectionFact,
    PreparedManifestArtifact | None,
] | None:
    if not isinstance(entry, TranscriptMessageLeafEntryFact):
        return None
    provider = entry.semantic_identity.message_provider_semantic_identity
    if provider.role != "user" or provider.name != "user":
        return None
    if not isinstance(entry.content, InlineNormalizedMessageContentFact):
        return None
    refs = tuple(ref for ref in entry.source_event_refs if ref.event_type == "RUN_START")
    if len(refs) != 1:
        return None
    ref = refs[0]
    if not (previous_keep_after_sequence < ref.sequence <= current_keep_after_sequence):
        return None
    source = event_lookup(ref.event_id)
    if not isinstance(source, RunStartEvent):
        raise ValueError("human evidence leaf lacks exact RunStart")
    current = source.current_user_message
    if current.source_kind != "host_user_input":
        return None
    text = current.text
    encoded = text.encode("utf-8")
    leaf_semantic = build_frozen_fact(
        CompactionHumanEvidenceLeafSemanticFact,
        schema_version="compaction_human_evidence_leaf_semantic.v1",
        source_kind="direct_human_input",
        message_provider_semantic_fingerprint=provider.semantic_fingerprint,
        text_semantic_fingerprint=context_fingerprint(
            "compaction-human-evidence-source-text:v1", text
        ),
        text_utf8_sha256=sha256(encoded).hexdigest(),
        text_utf8_bytes=len(encoded),
    )
    attribution = build_frozen_fact(
        CompactionHumanEvidenceLeafAttributionFact,
        schema_version="compaction_human_evidence_leaf_attribution.v1",
        leaf_reference=_leaf_reference(entry, runtime_session_id),
        exact_run_start_event_reference=ref,
        message_id=current.message_id,
        run_id=source.run_id,
        turn_id=source.turn_id,
        reply_id=source.reply_id,
        source_sequence=ref.sequence,
        leaf_semantic_fingerprint=leaf_semantic.semantic_fingerprint,
    )
    sanitized = sanitize_compaction_evidence(text)
    artifact: PreparedManifestArtifact | None = None
    if sanitized.text_utf8_bytes <= 8 * 1024:
        projection: CompactionHumanEvidenceSelectionProjectionFact = build_frozen_fact(
            CompactionHumanEvidenceInlineSelectionProjectionFact,
            schema_version="compaction_human_evidence_inline_selection_projection.v1",
            source_leaf_semantic_fingerprint=leaf_semantic.semantic_fingerprint,
            sanitizer_contract_fingerprint=SANITIZER_CONTRACT_FINGERPRINT,
            sanitized_full_text=sanitized.text,
            sanitized_full_text_sha256=sanitized.text_sha256,
            sanitized_full_text_utf8_bytes=sanitized.text_utf8_bytes,
            hard_size_disposition="selectable",
        )
    else:
        content_ref = _artifact_reference(
            artifact_kind="compaction-human-evidence-sanitized-content",
            media_type="text/plain; charset=utf-8",
            content=sanitized.text,
        )
        projection = build_frozen_fact(
            CompactionHumanEvidenceArtifactSelectionProjectionFact,
            schema_version="compaction_human_evidence_artifact_selection_projection.v1",
            source_leaf_semantic_fingerprint=leaf_semantic.semantic_fingerprint,
            sanitizer_contract_fingerprint=SANITIZER_CONTRACT_FINGERPRINT,
            sanitized_full_text_reference=content_ref,
            sanitized_full_text_sha256=sanitized.text_sha256,
            sanitized_full_text_utf8_bytes=sanitized.text_utf8_bytes,
            hard_size_disposition="permanently_oversize",
        )
        artifact = PreparedManifestArtifact(
            reference=content_ref,
            content=sanitized.text,
            semantic_metadata=freeze_json({
                "kind": "compaction-human-evidence-sanitized-content",
                "selection_projection_fingerprint": projection.selection_projection_fingerprint,
            }),
        )
    return leaf_semantic, attribution, projection, artifact


def _build_page(
    index: int,
    rows: tuple[
        tuple[
            CompactionHumanEvidenceLeafSemanticFact,
            CompactionHumanEvidenceLeafAttributionFact,
            CompactionHumanEvidenceSelectionProjectionFact,
        ],
        ...,
    ],
) -> CompactionHumanEvidenceManifestPageFact:
    semantics = tuple(item[0] for item in rows)
    attributions = tuple(item[1] for item in rows)
    projections = tuple(item[2] for item in rows)
    return build_frozen_fact(
        CompactionHumanEvidenceManifestPageFact,
        schema_version="compaction_human_evidence_manifest_page.v1",
        page_index=index,
        ordered_leaf_semantics=semantics,
        ordered_leaf_attributions=attributions,
        ordered_selection_projections=projections,
        first_source_sequence=attributions[0].source_sequence,
        last_source_sequence=attributions[-1].source_sequence,
        semantic_accumulator=_accumulate(
            "compaction-human-evidence-page-semantic:v1",
            tuple(item.semantic_fingerprint for item in semantics),
        ),
        attribution_accumulator=_accumulate(
            "compaction-human-evidence-page-attribution:v1",
            tuple(item.attribution_fingerprint for item in attributions),
        ),
        selection_projection_accumulator=_accumulate(
            "compaction-human-evidence-page-selection:v1",
            tuple(item.selection_projection_fingerprint for item in projections),
        ),
    )


def build_human_evidence_manifest_plan(
    *,
    runtime_session_id: str,
    authority_snapshot: GovernanceTranscriptAuthoritySnapshotLike,
    previous_keep_after_sequence: int,
    current_keep_after_sequence: int,
    current_through_sequence: int,
    predecessor_completed_event_id: str | None,
    event_lookup: Callable[[str], AgentEvent | None],
) -> CompactionHumanEvidenceManifestPlan:
    """Build an immutable, content-addressed manifest plan from one reducer snapshot."""

    reducer = authority_snapshot.reducer_evidence_snapshot
    rows: list[
        tuple[
            CompactionHumanEvidenceLeafSemanticFact,
            CompactionHumanEvidenceLeafAttributionFact,
            CompactionHumanEvidenceSelectionProjectionFact,
        ]
    ] = []
    content_artifacts: list[PreparedManifestArtifact] = []
    classified: list[tuple[int, str]] = []
    for entry in reducer.stable_entries:
        source_sequences = tuple(ref.sequence for ref in entry.source_event_refs)
        in_window = any(
            previous_keep_after_sequence < value <= current_keep_after_sequence
            for value in source_sequences
        )
        if not in_window:
            continue
        eligible = _eligible_human_leaf(
            entry=entry,
            previous_keep_after_sequence=previous_keep_after_sequence,
            current_keep_after_sequence=current_keep_after_sequence,
            event_lookup=event_lookup,
            runtime_session_id=runtime_session_id,
        )
        classified.append((entry.ordinal.value, "eligible" if eligible else "excluded"))
        if eligible is None:
            continue
        semantic, attribution, projection, artifact = eligible
        rows.append((semantic, attribution, projection))
        if artifact is not None:
            content_artifacts.append(artifact)

    pages: list[CompactionHumanEvidenceManifestPageFact] = []
    page_artifacts: list[PreparedManifestArtifact] = []
    offset = 0
    while offset < len(rows):
        count = min(256, len(rows) - offset)
        while count:
            page = _build_page(len(pages), tuple(rows[offset : offset + count]))
            encoded = canonical_json_bytes(page.model_dump(mode="json"))
            if len(encoded) <= 1024 * 1024:
                break
            count -= 1
        if count == 0:
            raise ValueError("one manifest leaf exceeds page hard bound")
        pages.append(page)
        page_content = encoded.decode("utf-8")
        page_ref = _artifact_reference(
            artifact_kind="compaction-human-evidence-manifest-page",
            media_type="application/vnd.pulsara.compaction-human-evidence-page+json",
            content=page_content,
        )
        page_artifacts.append(
            PreparedManifestArtifact(
                reference=page_ref,
                content=page_content,
                semantic_metadata=freeze_json({
                    "kind": "compaction-human-evidence-manifest-page",
                    "page_fingerprint": page.page_fingerprint,
                }),
            )
        )
        offset += count

    semantic_accumulator = _accumulate(
        "compaction-human-evidence-manifest-semantic:v1",
        tuple(item[0].semantic_fingerprint for item in rows),
    )
    attribution_accumulator = _accumulate(
        "compaction-human-evidence-manifest-attribution:v1",
        tuple(item[1].attribution_fingerprint for item in rows),
    )
    projection_accumulator = _accumulate(
        "compaction-human-evidence-manifest-selection:v1",
        tuple(item[2].selection_projection_fingerprint for item in rows),
    )
    completeness = context_fingerprint(
        "compaction-human-evidence-domain-completeness-proof:v1",
        {
            "snapshot": reducer.snapshot_fingerprint,
            "window": (previous_keep_after_sequence, current_keep_after_sequence),
            "classified": tuple(classified),
        },
    )
    transitive_coverage = _accumulate(
        "compaction-human-evidence-transitive-coverage:v1",
        tuple(item[0].semantic_fingerprint for item in rows),
    )
    semantic = build_frozen_fact(
        CompactionHumanEvidenceManifestSemanticFact,
        schema_version="compaction_human_evidence_manifest_semantic.v1",
        eligible_leaf_count=len(rows),
        ordered_semantic_accumulator=semantic_accumulator,
        transitive_leaf_coverage_fingerprint=transitive_coverage,
        selection_contract_fingerprint=MANIFEST_SOURCE_SELECTION_CONTRACT_FINGERPRINT,
    )
    live = reducer.live_state
    window = build_frozen_fact(
        CompactionHumanEvidenceSelectionWindowAttributionFact,
        schema_version="compaction_human_evidence_selection_window_attribution.v1",
        previous_keep_after_sequence=previous_keep_after_sequence,
        current_keep_after_sequence=current_keep_after_sequence,
        current_through_sequence=current_through_sequence,
        predecessor_completed_event_id=predecessor_completed_event_id,
        transcript_projection_base_semantic_fingerprint=(
            live.stable_semantic_state.state_semantic_fingerprint
        ),
        transcript_semantic_source_fingerprint=(
            live.stable_semantic_state.semantic_source_accumulator
        ),
        transcript_stable_state_semantic_fingerprint=(
            live.stable_semantic_state.state_semantic_fingerprint
        ),
        selection_contract_fingerprint=MANIFEST_SOURCE_SELECTION_CONTRACT_FINGERPRINT,
    )
    root = build_frozen_fact(
        CompactionHumanEvidenceManifestRootFact,
        schema_version="compaction_human_evidence_manifest_root.v1",
        ordered_page_references=tuple(item.reference for item in page_artifacts),
        page_count=len(pages),
        eligible_leaf_count=len(rows),
        ordered_semantic_accumulator=semantic_accumulator,
        transitive_leaf_coverage_fingerprint=transitive_coverage,
        source_selection_contract_fingerprint=(
            MANIFEST_SOURCE_SELECTION_CONTRACT_FINGERPRINT
        ),
        ordered_attribution_accumulator=attribution_accumulator,
        ordered_selection_projection_accumulator=projection_accumulator,
        first_source_sequence=rows[0][1].source_sequence if rows else None,
        last_source_sequence=rows[-1][1].source_sequence if rows else None,
        transcript_cursor_fingerprint=reducer.snapshot_fingerprint,
        runtime_session_id=runtime_session_id,
        selection_window_attribution=window,
        transcript_cursor_generation=0,
        verified_through_sequence=authority_snapshot.ledger_through_sequence,
        ledger_continuity_accumulator=(
            authority_snapshot.ledger_continuity_accumulator
        ),
        domain_completeness_proof_fingerprint=completeness,
        selection_projection_contract_fingerprint=(
            MANIFEST_SELECTION_PROJECTION_CONTRACT_FINGERPRINT
        ),
    )
    root_content = canonical_json_bytes(root.model_dump(mode="json")).decode("utf-8")
    root_ref = _artifact_reference(
        artifact_kind="compaction-human-evidence-manifest-root",
        media_type="application/vnd.pulsara.compaction-human-evidence-root+json",
        content=root_content,
    )
    root_artifact = PreparedManifestArtifact(
        reference=root_ref,
        content=root_content,
        semantic_metadata=freeze_json({
            "kind": "compaction-human-evidence-manifest-root",
            "root_fingerprint": root.root_fingerprint,
        }),
    )
    attribution = build_frozen_fact(
        CompactionHumanEvidenceManifestAttributionFact,
        schema_version="compaction_human_evidence_manifest_attribution.v1",
        manifest_semantic_fingerprint=semantic.manifest_semantic_fingerprint,
        runtime_session_id=runtime_session_id,
        selection_window_attribution=window,
        transcript_cursor_fingerprint=reducer.snapshot_fingerprint,
        transcript_cursor_generation=0,
        verified_through_sequence=authority_snapshot.ledger_through_sequence,
        ledger_continuity_accumulator=authority_snapshot.ledger_continuity_accumulator,
        domain_completeness_proof_fingerprint=completeness,
        ordered_leaf_attribution_accumulator=attribution_accumulator,
        ordered_selection_projection_accumulator=projection_accumulator,
        selection_projection_contract_fingerprint=(
            MANIFEST_SELECTION_PROJECTION_CONTRACT_FINGERPRINT
        ),
        paged_manifest_root_reference=root_ref,
    )
    reference = build_frozen_fact(
        CompactionHumanEvidenceManifestReferenceFact,
        schema_version="compaction_human_evidence_manifest_reference.v1",
        manifest_semantic_fingerprint=semantic.manifest_semantic_fingerprint,
        manifest_attribution_fingerprint=attribution.attribution_fingerprint,
        paged_manifest_root_reference=root_ref,
    )
    artifacts = tuple((*content_artifacts, *page_artifacts, root_artifact))
    return CompactionHumanEvidenceManifestPlan(
        semantic=semantic,
        attribution=attribution,
        reference=reference,
        root=root,
        pages=tuple(pages),
        artifacts=artifacts,
        plan_fingerprint=context_fingerprint(
            "compaction-human-evidence-manifest-plan:v1",
            {
                "reference": reference.reference_fingerprint,
                "artifacts": tuple(item.reference.reference_fingerprint for item in artifacts),
            },
        ),
    )


class ManifestPreparationOperation(CompactionHumanEvidenceManifestPreparationHandle):
    """One logical/physical owner for deterministic manifest artifact writes."""

    def __init__(
        self,
        *,
        identity: CompactionHumanEvidenceManifestPreparationIdentity,
        plan: CompactionHumanEvidenceManifestPlan,
        archive: ArtifactStore,
        runtime_session_id: str,
        physical_executor: Executor,
    ) -> None:
        self._identity = identity
        self._plan = plan
        self._archive = archive
        self._runtime_session_id = runtime_session_id
        self._physical_executor = physical_executor
        self._lock = RLock()
        self._logical_state = "preparing"
        self._physical_state = "queued"
        self._completion_consumed = False
        self._failure: CompactionHumanEvidenceManifestPreparationFailureSnapshot | None = None
        self._cancel_requested = False
        self._task: asyncio.Task[None] | None = None
        self._exit_callbacks: list[Callable[[], None]] = []

    @property
    def identity(self) -> CompactionHumanEvidenceManifestPreparationIdentity:
        return self._identity

    def start(self) -> None:
        with self._lock:
            if self._task is not None:
                raise RuntimeError("manifest preparation already started")
            self._task = asyncio.create_task(self._run())

    def add_exit_callback(self, callback: Callable[[], None]) -> None:
        call_now = False
        with self._lock:
            if self._physical_state == "exited":
                call_now = True
            else:
                self._exit_callbacks.append(callback)
        if call_now:
            callback()

    def snapshot_nowait(self) -> CompactionHumanEvidenceManifestPreparationSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def consume_full_or_abandon(
        self,
    ) -> CompactionHumanEvidenceManifestConsumptionOutcome:
        with self._lock:
            if self._completion_consumed:
                raise RuntimeError("manifest completion disposition was already consumed")
            self._completion_consumed = True
            if self._logical_state == "full":
                return _runtime_fact(
                    CompactionHumanEvidenceManifestConsumedFull,
                    "outcome_fingerprint",
                    "compaction-human-evidence-manifest-consumed-full:v1",
                    outcome_kind="full",
                    manifest_reference=self._plan.reference,
                    pin_transfer_identity_fingerprint=context_fingerprint(
                        "compaction-human-evidence-manifest-pin-transfer:v1",
                        (self._identity.identity_fingerprint, self._plan.reference.reference_fingerprint),
                    ),
                )
            stage = (
                "manifest_prepare"
                if self._logical_state == "failed"
                else "manifest_abandoned"
                if self._logical_state == "abandoned"
                else "manifest_not_ready_at_completion"
            )
            self._logical_state = "abandoned"
            self._cancel_requested = True
            diagnostic = (
                self._failure.diagnostic
                if self._failure is not None
                else build_bounded_runtime_failure_diagnostic(
                    error=RuntimeError(stage),
                    redaction_profile_id="durable_projection_job_error.v1",
                )
            )
            return _runtime_fact(
                CompactionHumanEvidenceManifestConsumedAbandoned,
                "outcome_fingerprint",
                "compaction-human-evidence-manifest-consumed-abandoned:v1",
                outcome_kind="abandoned",
                failure_stage=stage,
                diagnostic=diagnostic,
            )

    def request_physical_cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True

    async def wait_physical_exit(self, *, deadline_monotonic: float) -> bool:
        task = self._task
        if task is None:
            return True
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            return task.done()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except TimeoutError:
            return False
        return True

    async def _run(self) -> None:
        with self._lock:
            self._physical_state = "running"
        try:
            for artifact in self._plan.artifacts:
                with self._lock:
                    if self._cancel_requested:
                        break
                physical_write = asyncio.get_running_loop().run_in_executor(
                    self._physical_executor,
                    partial(
                        self._archive.put_text_if_absent_or_confirm_identical,
                        artifact.reference.artifact_id,
                        artifact.content,
                        session_id=self._runtime_session_id,
                        run_id=None,
                        media_type=artifact.reference.media_type,
                        semantic_metadata=thaw_json(artifact.semantic_metadata),
                        deadline_monotonic=(
                            self._identity.operation_deadline_monotonic
                        ),
                    ),
                )
                await _await_physical_exit(physical_write)
        except BaseException as exc:
            diagnostic = build_bounded_runtime_failure_diagnostic(
                error=exc,
                redaction_profile_id="durable_projection_job_error.v1",
            )
            failure = _runtime_fact(
                CompactionHumanEvidenceManifestPreparationFailureSnapshot,
                "failure_fingerprint",
                "compaction-human-evidence-manifest-preparation-failure:v1",
                failure_stage="artifact_confirmation",
                diagnostic=diagnostic,
            )
            with self._lock:
                self._failure = failure
                if self._logical_state == "preparing":
                    self._logical_state = "failed"
        finally:
            with self._lock:
                self._physical_state = "exited"
                if self._logical_state == "preparing":
                    self._logical_state = (
                        "abandoned" if self._cancel_requested else "full"
                    )
                callbacks = tuple(self._exit_callbacks)
                self._exit_callbacks.clear()
            for callback in callbacks:
                callback()

    def _snapshot_locked(self) -> CompactionHumanEvidenceManifestPreparationSnapshot:
        return _runtime_fact(
            CompactionHumanEvidenceManifestPreparationSnapshot,
            "snapshot_fingerprint",
            "compaction-human-evidence-manifest-preparation-snapshot:v1",
            preparation_identity_fingerprint=self._identity.identity_fingerprint,
            logical_state=self._logical_state,
            physical_state=self._physical_state,
            completion_consumed=self._completion_consumed,
            failure=self._failure,
        )


def _runtime_fact(cls, fingerprint_field: str, domain: str, **payload):
    payload[fingerprint_field] = context_fingerprint(domain, payload)
    return cls(**payload)


async def _await_physical_exit(future: asyncio.Future[object]) -> object:
    """Cancellation detaches the waiter only after the blocking owner exits."""

    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(future)
            break
        except asyncio.CancelledError as exc:
            cancellation = exc
            if future.done():
                result = future.result()
                break
    if cancellation is not None:
        raise cancellation
    return result


def build_manifest_preparation_identity(
    *, preparation_id: str, generation: int, plan: CompactionHumanEvidenceManifestPlan, deadline_monotonic: float
) -> CompactionHumanEvidenceManifestPreparationIdentity:
    return _runtime_fact(
        CompactionHumanEvidenceManifestPreparationIdentity,
        "identity_fingerprint",
        "compaction-human-evidence-manifest-preparation-identity:v1",
        preparation_id=preparation_id,
        generation=generation,
        stable_manifest_reference_fingerprint=plan.reference.reference_fingerprint,
        operation_deadline_monotonic=deadline_monotonic,
    )


__all__ = [
    "MANIFEST_ARTIFACT_CONTRACT_FINGERPRINT",
    "MANIFEST_SELECTION_PROJECTION_CONTRACT_FINGERPRINT",
    "MANIFEST_SOURCE_SELECTION_CONTRACT_FINGERPRINT",
    "CompactionHumanEvidenceManifestPlan",
    "ManifestPreparationOperation",
    "PreparedManifestArtifact",
    "build_human_evidence_manifest_plan",
    "build_manifest_preparation_identity",
]
