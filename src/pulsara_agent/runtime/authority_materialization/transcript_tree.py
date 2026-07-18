"""Content-addressed transcript trees and required run transcript seeds."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Iterable, Literal

from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives import context_fingerprint
from pulsara_agent.primitives._context_base import canonical_json_bytes
from pulsara_agent.primitives.authority_materialization import (
    AuthorityMaterializationLimits,
    TranscriptProjectionStableSemanticStateFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.transcript_projection import (
    EmptyTranscriptProjectionRootManifestFact,
    InlineNormalizedMessageContentFact,
    NonEmptyTranscriptProjectionRootManifestFact,
    NormalizedMessageContentArtifactContractFact,
    NormalizedMessageContentArtifactFact,
    NormalizedMessageContentArtifactReferenceFact,
    RunTranscriptSeedArtifactContractFact,
    RunTranscriptSeedArtifactFact,
    RunTranscriptSeedReferenceFact,
    RunTranscriptSeedSemanticFact,
    PreparedAuthorityArtifactWriteReservation,
    TranscriptMessageLeafEntryFact,
    TranscriptProjectionInternalNodeFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProjectionLeafNodeFact,
    TranscriptProjectionNodeRefFact,
    TranscriptProjectionRootManifestContractFact,
    TranscriptProjectionRootManifestFact,
    TranscriptProjectionRootManifestRefFact,
    TranscriptProjectionSemanticSourceFact,
    TranscriptProjectionTreeContractFact,
)


TRANSCRIPT_TREE_MEDIA_TYPE = (
    "application/vnd.pulsara.transcript-projection+json;version=1"
)
RUN_TRANSCRIPT_SEED_MEDIA_TYPE = (
    "application/vnd.pulsara.run-transcript-seed+json;version=2"
)
NORMALIZED_MESSAGE_CONTENT_MEDIA_TYPE = (
    "application/vnd.pulsara.normalized-message-content+json;version=1"
)


@dataclass(frozen=True, slots=True)
class TranscriptProjectionMaterializationContracts:
    tree: TranscriptProjectionTreeContractFact
    root_manifest: TranscriptProjectionRootManifestContractFact
    normalized_message_content: NormalizedMessageContentArtifactContractFact
    run_seed: RunTranscriptSeedArtifactContractFact


@dataclass(frozen=True, slots=True)
class PreparedContentAddressedArtifact:
    artifact_id: str
    canonical_bytes: bytes
    media_type: str
    semantic_metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class PreparedRunTranscriptSeed:
    seed_semantic: RunTranscriptSeedSemanticFact
    seed_reference: RunTranscriptSeedReferenceFact
    seed_artifact: RunTranscriptSeedArtifactFact
    root_manifest: TranscriptProjectionRootManifestFact
    root_reference: TranscriptProjectionRootManifestRefFact
    persisted_entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    artifacts: tuple[PreparedContentAddressedArtifact, ...]


@dataclass(frozen=True, slots=True)
class PreparedTranscriptProjectionMaterialization:
    root_manifest: TranscriptProjectionRootManifestFact
    root_reference: TranscriptProjectionRootManifestRefFact
    persisted_entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    artifacts: tuple[PreparedContentAddressedArtifact, ...]


def prepare_authority_artifact_write_reservation(
    *,
    operation_id: str,
    owner_kind: Literal[
        "run_seed_materialization", "checkpoint_materialization"
    ],
    artifacts: tuple[PreparedContentAddressedArtifact, ...],
    limits: AuthorityMaterializationLimits,
    absolute_deadline_monotonic: float,
) -> PreparedAuthorityArtifactWriteReservation:
    """Freeze the process-local physical shape before the first artifact write."""

    if not operation_id:
        raise ValueError("artifact write reservation requires an operation identity")
    if absolute_deadline_monotonic <= monotonic():
        raise TimeoutError("artifact write reservation deadline has already elapsed")
    artifact_count = len(artifacts)
    artifact_bytes = sum(len(item.canonical_bytes) for item in artifacts)
    artifact_batches = (
        artifact_count + limits.max_checkpoint_nodes_per_artifact_batch - 1
    ) // limits.max_checkpoint_nodes_per_artifact_batch
    max_artifact_count = (
        limits.max_checkpoint_changed_nodes_per_operation
        + limits.max_changed_message_content_artifacts_per_operation
        + (1 if owner_kind == "run_seed_materialization" else 0)
    )
    if artifact_count > max_artifact_count:
        raise ValueError("artifact write count exceeds its prepared reservation")
    if artifact_bytes > limits.max_checkpoint_total_artifact_bytes_per_operation:
        raise ValueError("artifact write bytes exceed their prepared reservation")
    if artifact_batches > limits.max_checkpoint_artifact_batches_per_operation:
        raise ValueError("artifact write batches exceed their prepared reservation")
    return PreparedAuthorityArtifactWriteReservation(
        operation_id=operation_id,
        owner_kind=owner_kind,
        max_artifact_count=max_artifact_count,
        max_artifact_bytes=limits.max_checkpoint_total_artifact_bytes_per_operation,
        max_artifact_batches=limits.max_checkpoint_artifact_batches_per_operation,
        absolute_deadline_monotonic=absolute_deadline_monotonic,
        limits_contract_fingerprint=limits.limits_contract_fingerprint,
    )


def build_default_transcript_projection_materialization_contracts(
    limits: AuthorityMaterializationLimits,
) -> TranscriptProjectionMaterializationContracts:
    ordinal_contract = context_fingerprint(
        "transcript-projection-ordinal-contract:v1",
        {"encoding": "u64_be_hex16", "minimum": 0, "maximum": 2**64 - 1},
    )
    tree = build_frozen_fact(
        TranscriptProjectionTreeContractFact,
        schema_version="transcript_projection_tree_contract.v1",
        tree_contract_id="pulsara.transcript-projection-tree",
        tree_contract_version="1",
        max_internal_fanout=32,
        max_leaf_entries=64,
        max_inline_entry_bytes=64 * 1024,
        max_node_bytes=limits.max_checkpoint_node_bytes,
        max_tree_height=4,
        maximum_representable_entries=limits.max_active_projection_entries,
        ordinal_contract_fingerprint=ordinal_contract,
        node_canonicalization_contract_fingerprint=context_fingerprint(
            "transcript-projection-node-canonicalization-contract:v1",
            "canonical-json-utf8+content-addressed-artifact",
        ),
        ordering_contract_fingerprint=context_fingerprint(
            "transcript-projection-tree-ordering-contract:v1",
            "outer-u64-ordinal-strict-increasing",
        ),
    )
    root = build_frozen_fact(
        TranscriptProjectionRootManifestContractFact,
        schema_version="transcript_projection_root_manifest_contract.v1",
        contract_id="pulsara.transcript-projection-root",
        contract_version="1",
        empty_root_schema_fingerprint=context_fingerprint(
            "transcript-projection-empty-root-schema:v2", "frozen"
        ),
        non_empty_root_schema_fingerprint=context_fingerprint(
            "transcript-projection-non-empty-root-schema:v2", "frozen"
        ),
        tree_contract_fingerprint=tree.tree_contract_fingerprint,
        normalized_transcript_fingerprint_contract_fingerprint=context_fingerprint(
            "normalized-transcript-fingerprint-contract:v1",
            "ordered-leaf-provider-semantic-fingerprints",
        ),
        root_canonicalization_contract_fingerprint=context_fingerprint(
            "transcript-projection-root-canonicalization-contract:v1",
            "canonical-json-utf8+empty-non-empty-union",
        ),
        max_root_manifest_bytes=limits.max_checkpoint_root_bytes,
    )
    message = build_frozen_fact(
        NormalizedMessageContentArtifactContractFact,
        schema_version="normalized_message_content_artifact_contract.v1",
        contract_id="pulsara.normalized-message-content",
        contract_version="1",
        document_schema_fingerprint=context_fingerprint(
            "normalized-message-content-document-schema:v1", "frozen"
        ),
        provider_message_semantic_contract_fingerprint=context_fingerprint(
            "transcript-message-provider-semantic-contract:v3", "frozen"
        ),
        provider_block_union_contract_fingerprint=context_fingerprint(
            "transcript-provider-block-union-contract:v1", "frozen"
        ),
        canonicalization_contract_fingerprint=context_fingerprint(
            "normalized-message-content-canonicalization-contract:v1",
            "canonical-json-utf8",
        ),
        max_document_bytes=limits.max_normalized_message_content_artifact_bytes,
        max_block_count=16_384,
    )
    seed = build_frozen_fact(
        RunTranscriptSeedArtifactContractFact,
        schema_version="run_transcript_seed_artifact_contract.v2",
        contract_id="pulsara.run-transcript-seed",
        contract_version="2",
        seed_artifact_schema_fingerprint=context_fingerprint(
            "run-transcript-seed-artifact-schema:v2", "frozen"
        ),
        root_manifest_contract_fingerprint=root.contract_fingerprint,
        canonicalization_contract_fingerprint=context_fingerprint(
            "run-transcript-seed-canonicalization-contract:v2",
            "canonical-json-utf8+bounded-root",
        ),
        max_seed_artifact_bytes=limits.max_checkpoint_root_bytes * 2,
    )
    return TranscriptProjectionMaterializationContracts(
        tree=tree,
        root_manifest=root,
        normalized_message_content=message,
        run_seed=seed,
    )


def prepare_run_transcript_seed(
    *,
    runtime_session_id: str,
    stable_state: TranscriptProjectionStableSemanticStateFact,
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    ledger_through_sequence: int,
    ledger_continuity_accumulator: str,
    reducer_id: str,
    reducer_version: str,
    reducer_contract_fingerprint: str,
    transcript_semantic_domain_contract_fingerprint: str,
    contracts: TranscriptProjectionMaterializationContracts,
    source_checkpoint_id: str | None = None,
) -> PreparedRunTranscriptSeed:
    if len(stable_entries) > contracts.tree.maximum_representable_entries:
        raise ValueError("transcript projection exceeds representable entry bound")
    normalized = _normalized_transcript_fingerprint(stable_entries)
    if normalized != stable_state.normalized_transcript_fingerprint:
        raise ValueError("stable entries do not match stable semantic state")

    materialization = prepare_transcript_projection_materialization(
        runtime_session_id=runtime_session_id,
        stable_entries=stable_entries,
        normalized_transcript_fingerprint=normalized,
        contracts=contracts,
    )
    source = build_frozen_fact(
        TranscriptProjectionSemanticSourceFact,
        schema_version="transcript_projection_semantic_source.v1",
        reducer_id=reducer_id,
        reducer_version=reducer_version,
        reducer_contract_fingerprint=reducer_contract_fingerprint,
        transcript_semantic_domain_contract_fingerprint=(
            transcript_semantic_domain_contract_fingerprint
        ),
        semantic_source_event_count=stable_state.semantic_source_event_count,
        semantic_source_accumulator=stable_state.semantic_source_accumulator,
        resulting_state_fingerprint=stable_state.state_semantic_fingerprint,
    )
    seed_semantic = build_frozen_fact(
        RunTranscriptSeedSemanticFact,
        schema_version="run_transcript_seed_semantic.v2",
        prior_semantic_source=source,
        prior_stable_semantic_state=stable_state,
        normalized_prior_transcript_fingerprint=normalized,
    )
    seed_artifact = build_frozen_fact(
        RunTranscriptSeedArtifactFact,
        schema_version="run_transcript_seed_artifact.v2",
        artifact_contract_fingerprint=contracts.run_seed.contract_fingerprint,
        seed_semantic=seed_semantic,
        root_manifest=materialization.root_manifest,
    )
    seed_bytes = canonical_json_bytes(seed_artifact.model_dump(mode="json"))
    if len(seed_bytes) > contracts.run_seed.max_seed_artifact_bytes:
        raise ValueError("run transcript seed artifact exceeds contract bound")
    seed_sha = _sha256(seed_bytes)
    seed_id = (
        "artifact:run-transcript-seed:"
        f"{_artifact_namespace(runtime_session_id)}:"
        f"{seed_sha.removeprefix('sha256:')}"
    )
    seed_reference = build_frozen_fact(
        RunTranscriptSeedReferenceFact,
        schema_version="run_transcript_seed_ref.v1",
        seed_artifact_id=seed_id,
        seed_artifact_sha256=seed_sha,
        seed_artifact_bytes=len(seed_bytes),
        seed_semantic_fingerprint=seed_semantic.seed_semantic_fingerprint,
        root_materialization_fingerprint=(
            materialization.root_manifest.materialization_fingerprint
        ),
        seed_artifact_contract_fingerprint=contracts.run_seed.contract_fingerprint,
        source_runtime_session_id=runtime_session_id,
        source_ledger_through_sequence=ledger_through_sequence,
        source_ledger_continuity_accumulator=ledger_continuity_accumulator,
        source_checkpoint_id=source_checkpoint_id,
    )
    seed_artifact_blob = PreparedContentAddressedArtifact(
        artifact_id=seed_id,
        canonical_bytes=seed_bytes,
        media_type=RUN_TRANSCRIPT_SEED_MEDIA_TYPE,
        semantic_metadata=_artifact_semantic_metadata({
            "artifact_kind": "run_transcript_seed",
            "seed_semantic_fingerprint": seed_semantic.seed_semantic_fingerprint,
            "root_materialization_fingerprint": (
                materialization.root_manifest.materialization_fingerprint
            ),
            "artifact_contract_fingerprint": contracts.run_seed.contract_fingerprint,
        }),
    )
    return PreparedRunTranscriptSeed(
        seed_semantic=seed_semantic,
        seed_reference=seed_reference,
        seed_artifact=seed_artifact,
        root_manifest=materialization.root_manifest,
        root_reference=materialization.root_reference,
        persisted_entries=materialization.persisted_entries,
        artifacts=(*materialization.artifacts, seed_artifact_blob),
    )


def prepare_transcript_projection_materialization(
    *,
    runtime_session_id: str,
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    normalized_transcript_fingerprint: str,
    contracts: TranscriptProjectionMaterializationContracts,
    previously_reachable_artifact_ids: frozenset[str] = frozenset(),
) -> PreparedTranscriptProjectionMaterialization:
    namespace = _artifact_namespace(runtime_session_id)
    persisted_entries, content_artifacts = _externalize_oversized_messages(
        stable_entries,
        contracts=contracts,
        artifact_namespace=namespace,
    )
    root, root_ref, tree_artifacts = _materialize_tree(
        persisted_entries,
        normalized_transcript_fingerprint=normalized_transcript_fingerprint,
        contracts=contracts,
        artifact_namespace=namespace,
    )
    artifacts = tuple(
        artifact
        for artifact in (*content_artifacts, *tree_artifacts)
        if artifact.artifact_id not in previously_reachable_artifact_ids
    )
    return PreparedTranscriptProjectionMaterialization(
        root_manifest=root,
        root_reference=root_ref,
        persisted_entries=persisted_entries,
        artifacts=artifacts,
    )


def persist_prepared_run_transcript_seed(
    prepared: PreparedRunTranscriptSeed,
    *,
    write_reservation: PreparedAuthorityArtifactWriteReservation,
    limits: AuthorityMaterializationLimits,
    archive: ArtifactStore,
    runtime_session_id: str,
    deadline_monotonic: float,
) -> None:
    _validate_artifact_write_reservation(
        reservation=write_reservation,
        expected_owner_kind="run_seed_materialization",
        artifacts=prepared.artifacts,
        limits=limits,
        deadline_monotonic=deadline_monotonic,
    )
    for artifact in prepared.artifacts:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("run transcript seed materialization deadline exceeded")
        archive.put_text_if_absent_or_confirm_identical(
            artifact.artifact_id,
            artifact.canonical_bytes.decode("utf-8"),
            session_id=runtime_session_id,
            run_id=None,
            media_type=artifact.media_type,
            semantic_metadata=artifact.semantic_metadata,
            deadline_monotonic=deadline_monotonic,
        )


def persist_prepared_transcript_projection_materialization(
    prepared: PreparedTranscriptProjectionMaterialization,
    *,
    write_reservation: PreparedAuthorityArtifactWriteReservation,
    limits: AuthorityMaterializationLimits,
    archive: ArtifactStore,
    runtime_session_id: str,
    run_id: str,
    deadline_monotonic: float,
) -> None:
    """Write only the COW artifact set prepared for one checkpoint attempt."""

    _validate_artifact_write_reservation(
        reservation=write_reservation,
        expected_owner_kind="checkpoint_materialization",
        artifacts=prepared.artifacts,
        limits=limits,
        deadline_monotonic=deadline_monotonic,
    )
    for artifact in prepared.artifacts:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("transcript checkpoint materialization deadline exceeded")
        archive.put_text_if_absent_or_confirm_identical(
            artifact.artifact_id,
            artifact.canonical_bytes.decode("utf-8"),
            session_id=runtime_session_id,
            run_id=run_id,
            media_type=artifact.media_type,
            semantic_metadata=artifact.semantic_metadata,
            deadline_monotonic=deadline_monotonic,
        )


def _validate_artifact_write_reservation(
    *,
    reservation: PreparedAuthorityArtifactWriteReservation,
    expected_owner_kind: Literal[
        "run_seed_materialization", "checkpoint_materialization"
    ],
    artifacts: tuple[PreparedContentAddressedArtifact, ...],
    limits: AuthorityMaterializationLimits,
    deadline_monotonic: float,
) -> None:
    if reservation.owner_kind != expected_owner_kind:
        raise ValueError("artifact write reservation owner kind mismatch")
    if reservation.limits_contract_fingerprint != limits.limits_contract_fingerprint:
        raise ValueError("artifact write reservation limits contract drifted")
    if reservation.absolute_deadline_monotonic != deadline_monotonic:
        raise ValueError("artifact write reservation deadline drifted")
    artifact_count = len(artifacts)
    artifact_bytes = sum(len(item.canonical_bytes) for item in artifacts)
    artifact_batches = (
        artifact_count + limits.max_checkpoint_nodes_per_artifact_batch - 1
    ) // limits.max_checkpoint_nodes_per_artifact_batch
    if artifact_count > reservation.max_artifact_count:
        raise ValueError("artifact count exceeds prepared reservation")
    if artifact_bytes > reservation.max_artifact_bytes:
        raise ValueError("artifact bytes exceed prepared reservation")
    if artifact_batches > reservation.max_artifact_batches:
        raise ValueError("artifact batches exceed prepared reservation")


def _externalize_oversized_messages(
    entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    *,
    contracts: TranscriptProjectionMaterializationContracts,
    artifact_namespace: str,
) -> tuple[
    tuple[TranscriptProjectionLeafEntryFact, ...],
    tuple[PreparedContentAddressedArtifact, ...],
]:
    persisted: list[TranscriptProjectionLeafEntryFact] = []
    artifacts: list[PreparedContentAddressedArtifact] = []
    for entry in entries:
        encoded = canonical_json_bytes(entry.model_dump(mode="json"))
        if len(encoded) <= contracts.tree.max_inline_entry_bytes:
            persisted.append(entry)
            continue
        if not isinstance(entry, TranscriptMessageLeafEntryFact) or not isinstance(
            entry.content, InlineNormalizedMessageContentFact
        ):
            raise ValueError("oversized projection entry has no artifact carrier")
        document = build_frozen_fact(
            NormalizedMessageContentArtifactFact,
            schema_version="normalized_message_content_artifact.v1",
            artifact_contract_fingerprint=(
                contracts.normalized_message_content.contract_fingerprint
            ),
            provider_semantic_identity=entry.content.provider_semantic_identity,
            blocks=entry.content.blocks,
        )
        document_bytes = canonical_json_bytes(document.model_dump(mode="json"))
        if len(document_bytes) > contracts.normalized_message_content.max_document_bytes:
            raise ValueError("normalized message content exceeds artifact contract")
        digest = _sha256(document_bytes)
        artifact_id = (
            f"artifact:normalized-message-content:{artifact_namespace}:"
            + digest.removeprefix("sha256:")
        )
        reference = build_frozen_fact(
            NormalizedMessageContentArtifactReferenceFact,
            schema_version="normalized_message_content_artifact_ref.v1",
            content_kind="normalized_message_artifact_ref",
            provider_semantic_identity=document.provider_semantic_identity,
            document_fact_fingerprint=document.fact_fingerprint,
            document_artifact_id=artifact_id,
            document_sha256=digest,
            document_byte_count=len(document_bytes),
            artifact_contract_fingerprint=(
                contracts.normalized_message_content.contract_fingerprint
            ),
        )
        persisted.append(
            build_frozen_fact(
                TranscriptMessageLeafEntryFact,
                schema_version="transcript_message_leaf_entry.v4",
                entry_kind="message",
                ordinal=entry.ordinal,
                semantic_identity=entry.semantic_identity,
                attribution=entry.attribution,
                content=reference,
                source_event_refs=entry.source_event_refs,
            )
        )
        artifacts.append(
            PreparedContentAddressedArtifact(
                artifact_id=artifact_id,
                canonical_bytes=document_bytes,
                media_type=NORMALIZED_MESSAGE_CONTENT_MEDIA_TYPE,
                semantic_metadata=_artifact_semantic_metadata({
                    "artifact_kind": "normalized_message_content",
                    "document_fact_fingerprint": document.fact_fingerprint,
                    "artifact_contract_fingerprint": (
                        contracts.normalized_message_content.contract_fingerprint
                    ),
                }),
            )
        )
    return tuple(persisted), tuple(artifacts)


def _materialize_tree(
    entries: tuple[TranscriptProjectionLeafEntryFact, ...],
    *,
    normalized_transcript_fingerprint: str,
    contracts: TranscriptProjectionMaterializationContracts,
    artifact_namespace: str,
) -> tuple[
    TranscriptProjectionRootManifestFact,
    TranscriptProjectionRootManifestRefFact,
    tuple[PreparedContentAddressedArtifact, ...],
]:
    artifacts: list[PreparedContentAddressedArtifact] = []
    if not entries:
        root: TranscriptProjectionRootManifestFact = build_frozen_fact(
            EmptyTranscriptProjectionRootManifestFact,
            schema_version="empty_transcript_projection_root.v2",
            root_kind="empty",
            root_manifest_contract_fingerprint=(
                contracts.root_manifest.contract_fingerprint
            ),
            tree_contract_fingerprint=contracts.tree.tree_contract_fingerprint,
            total_entry_count=0,
            normalized_transcript_fingerprint=normalized_transcript_fingerprint,
        )
    else:
        refs: list[TranscriptProjectionNodeRefFact] = []
        for group in _chunks(entries, contracts.tree.max_leaf_entries):
            node = build_frozen_fact(
                TranscriptProjectionLeafNodeFact,
                schema_version="transcript_projection_leaf_node.v1",
                first_ordinal=group[0].ordinal,
                entries=tuple(group),
                subtree_semantic_fingerprint=_subtree_fingerprint(
                    tuple(item.semantic_identity.semantic_fingerprint for item in group)
                ),
            )
            ref, artifact = _node_artifact(
                node,
                node_kind="leaf",
                first_ordinal=group[0].ordinal,
                last_ordinal=group[-1].ordinal,
                subtree_entry_count=len(group),
                subtree_semantic_fingerprint=node.subtree_semantic_fingerprint,
                contracts=contracts,
                artifact_namespace=artifact_namespace,
            )
            refs.append(ref)
            artifacts.append(artifact)
        tree_height = 1
        while len(refs) > 1:
            tree_height += 1
            if tree_height > contracts.tree.max_tree_height:
                raise ValueError("transcript projection tree exceeds height bound")
            parents: list[TranscriptProjectionNodeRefFact] = []
            for group in _balanced_groups(refs, contracts.tree.max_internal_fanout):
                node = build_frozen_fact(
                    TranscriptProjectionInternalNodeFact,
                    schema_version="transcript_projection_internal_node.v1",
                    tree_level=tree_height,
                    child_refs=tuple(group),
                    subtree_semantic_fingerprint=_subtree_fingerprint(
                        tuple(item.subtree_semantic_fingerprint for item in group)
                    ),
                )
                ref, artifact = _node_artifact(
                    node,
                    node_kind="internal",
                    first_ordinal=group[0].first_ordinal,
                    last_ordinal=group[-1].last_ordinal,
                    subtree_entry_count=sum(item.subtree_entry_count for item in group),
                    subtree_semantic_fingerprint=node.subtree_semantic_fingerprint,
                    contracts=contracts,
                    artifact_namespace=artifact_namespace,
                )
                parents.append(ref)
                artifacts.append(artifact)
            refs = parents
        root = build_frozen_fact(
            NonEmptyTranscriptProjectionRootManifestFact,
            schema_version="non_empty_transcript_projection_root.v2",
            root_kind="non_empty",
            root_manifest_contract_fingerprint=(
                contracts.root_manifest.contract_fingerprint
            ),
            tree_contract_fingerprint=contracts.tree.tree_contract_fingerprint,
            root_node_ref=refs[0],
            tree_height=tree_height,
            total_entry_count=len(entries),
            normalized_transcript_fingerprint=normalized_transcript_fingerprint,
        )
    root_bytes = canonical_json_bytes(root.model_dump(mode="json"))
    if len(root_bytes) > contracts.root_manifest.max_root_manifest_bytes:
        raise ValueError("transcript root manifest exceeds contract bound")
    root_sha = _sha256(root_bytes)
    root_id = (
        f"artifact:transcript-root:{artifact_namespace}:"
        f"{root_sha.removeprefix('sha256:')}"
    )
    root_ref = build_frozen_fact(
        TranscriptProjectionRootManifestRefFact,
        schema_version="transcript_projection_root_ref.v3",
        root_kind=root.root_kind,
        root_artifact_id=root_id,
        root_sha256=root_sha,
        root_byte_count=len(root_bytes),
        normalized_transcript_fingerprint=normalized_transcript_fingerprint,
        materialization_fingerprint=root.materialization_fingerprint,
        root_manifest_contract_fingerprint=contracts.root_manifest.contract_fingerprint,
    )
    artifacts.append(
        PreparedContentAddressedArtifact(
            artifact_id=root_id,
            canonical_bytes=root_bytes,
            media_type=TRANSCRIPT_TREE_MEDIA_TYPE,
            semantic_metadata=_artifact_semantic_metadata({
                "artifact_kind": "transcript_projection_root",
                "root_kind": root.root_kind,
                "materialization_fingerprint": root.materialization_fingerprint,
                "root_manifest_contract_fingerprint": (
                    contracts.root_manifest.contract_fingerprint
                ),
            }),
        )
    )
    return root, root_ref, tuple(artifacts)


def _node_artifact(
    node: TranscriptProjectionLeafNodeFact | TranscriptProjectionInternalNodeFact,
    *,
    node_kind: Literal["internal", "leaf"],
    first_ordinal,
    last_ordinal,
    subtree_entry_count: int,
    subtree_semantic_fingerprint: str,
    contracts: TranscriptProjectionMaterializationContracts,
    artifact_namespace: str,
) -> tuple[TranscriptProjectionNodeRefFact, PreparedContentAddressedArtifact]:
    node_bytes = canonical_json_bytes(node.model_dump(mode="json"))
    if len(node_bytes) > contracts.tree.max_node_bytes:
        raise ValueError("transcript projection node exceeds contract bound")
    digest = _sha256(node_bytes)
    artifact_id = (
        f"artifact:transcript-node:{artifact_namespace}:"
        f"{digest.removeprefix('sha256:')}"
    )
    ref = build_frozen_fact(
        TranscriptProjectionNodeRefFact,
        schema_version="transcript_projection_node_ref.v1",
        node_kind=node_kind,
        node_artifact_id=artifact_id,
        node_sha256=digest,
        node_byte_count=len(node_bytes),
        first_ordinal=first_ordinal,
        last_ordinal=last_ordinal,
        subtree_entry_count=subtree_entry_count,
        subtree_semantic_fingerprint=subtree_semantic_fingerprint,
    )
    return ref, PreparedContentAddressedArtifact(
        artifact_id=artifact_id,
        canonical_bytes=node_bytes,
        media_type=TRANSCRIPT_TREE_MEDIA_TYPE,
        semantic_metadata=_artifact_semantic_metadata({
            "artifact_kind": f"transcript_projection_{node_kind}_node",
            "node_fingerprint": node.node_fingerprint,
            "tree_contract_fingerprint": contracts.tree.tree_contract_fingerprint,
        }),
    )


def _normalized_transcript_fingerprint(
    entries: Iterable[TranscriptProjectionLeafEntryFact],
) -> str:
    return context_fingerprint(
        "normalized-transcript-semantic:v1",
        tuple(item.semantic_identity.semantic_fingerprint for item in entries),
    )


def _subtree_fingerprint(fingerprints: tuple[str, ...]) -> str:
    return context_fingerprint("transcript-projection-subtree-semantic:v1", fingerprints)


def _sha256(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _artifact_semantic_metadata(
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        **payload,
        "semantic_metadata_fingerprint": context_fingerprint(
            "transcript-projection-artifact-semantic-metadata:v1",
            payload,
        ),
    }


def _artifact_namespace(runtime_session_id: str) -> str:
    return sha256(runtime_session_id.encode("utf-8")).hexdigest()[:16]


def _chunks(values: tuple, size: int) -> Iterable[tuple]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _balanced_groups(values: list, maximum: int) -> tuple[tuple, ...]:
    if len(values) <= maximum:
        return (tuple(values),)
    groups = [
        tuple(values[index : index + maximum])
        for index in range(0, len(values), maximum)
    ]
    if len(groups[-1]) == 1:
        previous = groups[-2]
        groups[-2] = previous[:-1]
        groups[-1] = (previous[-1], *groups[-1])
    return tuple(groups)


__all__ = [
    "PreparedContentAddressedArtifact",
    "PreparedRunTranscriptSeed",
    "PreparedTranscriptProjectionMaterialization",
    "TranscriptProjectionMaterializationContracts",
    "build_default_transcript_projection_materialization_contracts",
    "persist_prepared_run_transcript_seed",
    "persist_prepared_transcript_projection_materialization",
    "prepare_authority_artifact_write_reservation",
    "prepare_run_transcript_seed",
    "prepare_transcript_projection_materialization",
]
