"""Strict hydration for run transcript seeds and projection trees."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from time import monotonic

from pydantic import TypeAdapter, ValidationError

from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.transcript_projection import (
    EmptyTranscriptProjectionRootManifestFact,
    NonEmptyTranscriptProjectionRootManifestFact,
    NormalizedMessageContentArtifactFact,
    NormalizedMessageContentArtifactReferenceFact,
    RunTranscriptSeedArtifactFact,
    RunTranscriptSeedReferenceFact,
    RunTranscriptSeedSemanticFact,
    TranscriptMessageLeafEntryFact,
    TranscriptProjectionInternalNodeFact,
    TranscriptProjectionLeafEntryFact,
    TranscriptProjectionLeafNodeFact,
    TranscriptProjectionNodeRefFact,
    TranscriptProjectionRootManifestFact,
    TranscriptProjectionRootManifestRefFact,
)
from pulsara_agent.runtime.authority_materialization.transcript_tree import (
    NORMALIZED_MESSAGE_CONTENT_MEDIA_TYPE,
    RUN_TRANSCRIPT_SEED_MEDIA_TYPE,
    TRANSCRIPT_TREE_MEDIA_TYPE,
    TranscriptProjectionMaterializationContracts,
)


class TranscriptProjectionHydrationError(RuntimeError):
    """A durable projection artifact cannot be proven against its references."""


@dataclass(frozen=True, slots=True)
class HydratedRunTranscriptSeed:
    seed_artifact: RunTranscriptSeedArtifactFact
    root_manifest: TranscriptProjectionRootManifestFact
    entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    hydrated_message_contents: tuple[NormalizedMessageContentArtifactFact, ...]
    reachable_artifact_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class HydratedTranscriptProjectionMaterialization:
    root_manifest: TranscriptProjectionRootManifestFact
    entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    hydrated_message_contents: tuple[NormalizedMessageContentArtifactFact, ...]
    reachable_artifact_ids: frozenset[str]


_NODE_ADAPTER = TypeAdapter(
    TranscriptProjectionLeafNodeFact | TranscriptProjectionInternalNodeFact
)


def hydrate_run_transcript_seed(
    *,
    archive: ArtifactStore,
    runtime_session_id: str,
    seed_semantic: RunTranscriptSeedSemanticFact,
    seed_reference: RunTranscriptSeedReferenceFact,
    contracts: TranscriptProjectionMaterializationContracts,
    deadline_monotonic: float,
) -> HydratedRunTranscriptSeed:
    if seed_reference.source_runtime_session_id != runtime_session_id:
        raise TranscriptProjectionHydrationError("run seed source ledger mismatch")
    if seed_reference.seed_semantic_fingerprint != (
        seed_semantic.seed_semantic_fingerprint
    ):
        raise TranscriptProjectionHydrationError("run seed semantic reference mismatch")
    if seed_reference.seed_artifact_contract_fingerprint != (
        contracts.run_seed.contract_fingerprint
    ):
        raise TranscriptProjectionHydrationError("run seed artifact contract mismatch")

    payload = _read_verified_text_artifact(
        archive=archive,
        runtime_session_id=runtime_session_id,
        artifact_id=seed_reference.seed_artifact_id,
        expected_sha256=seed_reference.seed_artifact_sha256,
        expected_bytes=seed_reference.seed_artifact_bytes,
        expected_media_type=RUN_TRANSCRIPT_SEED_MEDIA_TYPE,
        max_bytes=contracts.run_seed.max_seed_artifact_bytes,
        deadline_monotonic=deadline_monotonic,
    )
    try:
        artifact = RunTranscriptSeedArtifactFact.model_validate_json(payload)
    except ValidationError as exc:
        raise TranscriptProjectionHydrationError("run seed artifact is invalid") from exc
    if artifact.seed_semantic != seed_semantic:
        raise TranscriptProjectionHydrationError("run seed artifact semantic mismatch")
    if artifact.artifact_contract_fingerprint != contracts.run_seed.contract_fingerprint:
        raise TranscriptProjectionHydrationError("run seed document contract mismatch")
    root = artifact.root_manifest
    if root.materialization_fingerprint != (
        seed_reference.root_materialization_fingerprint
    ):
        raise TranscriptProjectionHydrationError("run seed root identity mismatch")
    _validate_root_contract(root, contracts=contracts)

    entries: tuple[TranscriptProjectionLeafEntryFact, ...]
    reachable_artifact_ids = {seed_reference.seed_artifact_id}
    if isinstance(root, EmptyTranscriptProjectionRootManifestFact):
        entries = ()
    else:
        entries = _hydrate_node(
            archive=archive,
            runtime_session_id=runtime_session_id,
            reference=root.root_node_ref,
            contracts=contracts,
            expected_tree_level=root.tree_height,
            deadline_monotonic=deadline_monotonic,
            reachable_artifact_ids=reachable_artifact_ids,
        )
    _validate_hydrated_entries(root, entries)

    contents: list[NormalizedMessageContentArtifactFact] = []
    for entry in entries:
        if not isinstance(entry, TranscriptMessageLeafEntryFact) or not isinstance(
            entry.content,
            NormalizedMessageContentArtifactReferenceFact,
        ):
            continue
        contents.append(
            _hydrate_message_content(
                archive=archive,
                runtime_session_id=runtime_session_id,
                reference=entry.content,
                contracts=contracts,
                deadline_monotonic=deadline_monotonic,
            )
        )
        reachable_artifact_ids.add(entry.content.document_artifact_id)
    return HydratedRunTranscriptSeed(
        seed_artifact=artifact,
        root_manifest=root,
        entries=entries,
        hydrated_message_contents=tuple(contents),
        reachable_artifact_ids=frozenset(reachable_artifact_ids),
    )


def hydrate_transcript_projection_materialization(
    *,
    archive: ArtifactStore,
    runtime_session_id: str,
    root_reference: TranscriptProjectionRootManifestRefFact,
    contracts: TranscriptProjectionMaterializationContracts,
    deadline_monotonic: float,
) -> HydratedTranscriptProjectionMaterialization:
    """Hydrate a checkpoint root and prove all reachable immutable content."""

    if root_reference.root_manifest_contract_fingerprint != (
        contracts.root_manifest.contract_fingerprint
    ):
        raise TranscriptProjectionHydrationError(
            "transcript checkpoint root contract mismatch"
        )
    payload = _read_verified_text_artifact(
        archive=archive,
        runtime_session_id=runtime_session_id,
        artifact_id=root_reference.root_artifact_id,
        expected_sha256=root_reference.root_sha256,
        expected_bytes=root_reference.root_byte_count,
        expected_media_type=TRANSCRIPT_TREE_MEDIA_TYPE,
        max_bytes=contracts.root_manifest.max_root_manifest_bytes,
        deadline_monotonic=deadline_monotonic,
    )
    try:
        root = TypeAdapter(TranscriptProjectionRootManifestFact).validate_json(payload)
    except ValidationError as exc:
        raise TranscriptProjectionHydrationError(
            "transcript checkpoint root is invalid"
        ) from exc
    _validate_root_contract(root, contracts=contracts)
    if (
        root.root_kind != root_reference.root_kind
        or root.normalized_transcript_fingerprint
        != root_reference.normalized_transcript_fingerprint
        or root.materialization_fingerprint
        != root_reference.materialization_fingerprint
    ):
        raise TranscriptProjectionHydrationError(
            "transcript checkpoint root reference mismatch"
        )

    reachable_artifact_ids = {root_reference.root_artifact_id}
    if isinstance(root, EmptyTranscriptProjectionRootManifestFact):
        entries: tuple[TranscriptProjectionLeafEntryFact, ...] = ()
    else:
        entries = _hydrate_node(
            archive=archive,
            runtime_session_id=runtime_session_id,
            reference=root.root_node_ref,
            contracts=contracts,
            expected_tree_level=root.tree_height,
            deadline_monotonic=deadline_monotonic,
            reachable_artifact_ids=reachable_artifact_ids,
        )
    _validate_hydrated_entries(root, entries)

    contents: list[NormalizedMessageContentArtifactFact] = []
    for entry in entries:
        if not isinstance(entry, TranscriptMessageLeafEntryFact) or not isinstance(
            entry.content,
            NormalizedMessageContentArtifactReferenceFact,
        ):
            continue
        contents.append(
            _hydrate_message_content(
                archive=archive,
                runtime_session_id=runtime_session_id,
                reference=entry.content,
                contracts=contracts,
                deadline_monotonic=deadline_monotonic,
            )
        )
        reachable_artifact_ids.add(entry.content.document_artifact_id)
    return HydratedTranscriptProjectionMaterialization(
        root_manifest=root,
        entries=entries,
        hydrated_message_contents=tuple(contents),
        reachable_artifact_ids=frozenset(reachable_artifact_ids),
    )


def _hydrate_node(
    *,
    archive: ArtifactStore,
    runtime_session_id: str,
    reference: TranscriptProjectionNodeRefFact,
    contracts: TranscriptProjectionMaterializationContracts,
    expected_tree_level: int,
    deadline_monotonic: float,
    reachable_artifact_ids: set[str],
) -> tuple[TranscriptProjectionLeafEntryFact, ...]:
    reachable_artifact_ids.add(reference.node_artifact_id)
    payload = _read_verified_text_artifact(
        archive=archive,
        runtime_session_id=runtime_session_id,
        artifact_id=reference.node_artifact_id,
        expected_sha256=reference.node_sha256,
        expected_bytes=reference.node_byte_count,
        expected_media_type=TRANSCRIPT_TREE_MEDIA_TYPE,
        max_bytes=contracts.tree.max_node_bytes,
        deadline_monotonic=deadline_monotonic,
    )
    try:
        node = _NODE_ADAPTER.validate_json(payload)
    except ValidationError as exc:
        raise TranscriptProjectionHydrationError("transcript tree node is invalid") from exc

    if isinstance(node, TranscriptProjectionLeafNodeFact):
        if reference.node_kind != "leaf" or expected_tree_level != 1:
            raise TranscriptProjectionHydrationError("transcript leaf level mismatch")
        entries = node.entries
        subtree = _subtree_fingerprint(
            tuple(item.semantic_identity.semantic_fingerprint for item in entries)
        )
    else:
        if reference.node_kind != "internal" or node.tree_level != expected_tree_level:
            raise TranscriptProjectionHydrationError("transcript internal level mismatch")
        if len(node.child_refs) > contracts.tree.max_internal_fanout:
            raise TranscriptProjectionHydrationError("transcript tree fanout exceeds contract")
        nested: list[TranscriptProjectionLeafEntryFact] = []
        for child in node.child_refs:
            nested.extend(
                _hydrate_node(
                    archive=archive,
                    runtime_session_id=runtime_session_id,
                    reference=child,
                    contracts=contracts,
                    expected_tree_level=expected_tree_level - 1,
                    deadline_monotonic=deadline_monotonic,
                    reachable_artifact_ids=reachable_artifact_ids,
                )
            )
        entries = tuple(nested)
        subtree = _subtree_fingerprint(
            tuple(item.subtree_semantic_fingerprint for item in node.child_refs)
        )
    if node.subtree_semantic_fingerprint != subtree:
        raise TranscriptProjectionHydrationError("transcript node semantic mismatch")
    if len(entries) != reference.subtree_entry_count:
        raise TranscriptProjectionHydrationError("transcript node entry count mismatch")
    if not entries:
        raise TranscriptProjectionHydrationError("transcript node cannot hydrate empty")
    if (
        entries[0].ordinal != reference.first_ordinal
        or entries[-1].ordinal != reference.last_ordinal
        or reference.subtree_semantic_fingerprint != subtree
    ):
        raise TranscriptProjectionHydrationError("transcript node reference mismatch")
    return entries


def _hydrate_message_content(
    *,
    archive: ArtifactStore,
    runtime_session_id: str,
    reference: NormalizedMessageContentArtifactReferenceFact,
    contracts: TranscriptProjectionMaterializationContracts,
    deadline_monotonic: float,
) -> NormalizedMessageContentArtifactFact:
    if reference.artifact_contract_fingerprint != (
        contracts.normalized_message_content.contract_fingerprint
    ):
        raise TranscriptProjectionHydrationError("message content contract mismatch")
    payload = _read_verified_text_artifact(
        archive=archive,
        runtime_session_id=runtime_session_id,
        artifact_id=reference.document_artifact_id,
        expected_sha256=reference.document_sha256,
        expected_bytes=reference.document_byte_count,
        expected_media_type=NORMALIZED_MESSAGE_CONTENT_MEDIA_TYPE,
        max_bytes=contracts.normalized_message_content.max_document_bytes,
        deadline_monotonic=deadline_monotonic,
    )
    try:
        document = NormalizedMessageContentArtifactFact.model_validate_json(payload)
    except ValidationError as exc:
        raise TranscriptProjectionHydrationError(
            "normalized message content artifact is invalid"
        ) from exc
    if (
        document.fact_fingerprint != reference.document_fact_fingerprint
        or document.provider_semantic_identity != reference.provider_semantic_identity
        or document.artifact_contract_fingerprint
        != contracts.normalized_message_content.contract_fingerprint
    ):
        raise TranscriptProjectionHydrationError(
            "normalized message content reference mismatch"
        )
    return document


def _read_verified_text_artifact(
    *,
    archive: ArtifactStore,
    runtime_session_id: str,
    artifact_id: str,
    expected_sha256: str,
    expected_bytes: int,
    expected_media_type: str,
    max_bytes: int,
    deadline_monotonic: float,
) -> str:
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("transcript projection hydration deadline exceeded")
    info = archive.get_info(
        artifact_id,
        session_id=runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    if (
        info.digest != expected_sha256
        or info.size_bytes != expected_bytes
        or info.media_type != expected_media_type
        or info.size_bytes > max_bytes
    ):
        raise TranscriptProjectionHydrationError("artifact record identity mismatch")
    text = archive.get_text(
        artifact_id,
        session_id=runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    payload = text.encode("utf-8")
    if len(payload) != expected_bytes or _sha256(payload) != expected_sha256:
        raise TranscriptProjectionHydrationError("artifact content identity mismatch")
    return text


def _validate_root_contract(
    root: TranscriptProjectionRootManifestFact,
    *,
    contracts: TranscriptProjectionMaterializationContracts,
) -> None:
    if (
        root.root_manifest_contract_fingerprint
        != contracts.root_manifest.contract_fingerprint
        or root.tree_contract_fingerprint != contracts.tree.tree_contract_fingerprint
    ):
        raise TranscriptProjectionHydrationError("transcript root contract mismatch")
    if isinstance(root, NonEmptyTranscriptProjectionRootManifestFact):
        if root.tree_height > contracts.tree.max_tree_height:
            raise TranscriptProjectionHydrationError("transcript tree height exceeds contract")
        if root.total_entry_count > contracts.tree.maximum_representable_entries:
            raise TranscriptProjectionHydrationError("transcript tree count exceeds contract")


def _validate_hydrated_entries(
    root: TranscriptProjectionRootManifestFact,
    entries: tuple[TranscriptProjectionLeafEntryFact, ...],
) -> None:
    values = tuple(item.ordinal.value for item in entries)
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise TranscriptProjectionHydrationError("transcript ordinals are not canonical")
    if len(entries) != root.total_entry_count:
        raise TranscriptProjectionHydrationError("transcript root entry count mismatch")
    normalized = context_fingerprint(
        "normalized-transcript-semantic:v1",
        tuple(item.semantic_identity.semantic_fingerprint for item in entries),
    )
    if normalized != root.normalized_transcript_fingerprint:
        raise TranscriptProjectionHydrationError("transcript root semantic mismatch")


def _subtree_fingerprint(values: tuple[str, ...]) -> str:
    return context_fingerprint("transcript-projection-subtree-semantic:v1", values)


def _sha256(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


__all__ = [
    "HydratedRunTranscriptSeed",
    "HydratedTranscriptProjectionMaterialization",
    "TranscriptProjectionHydrationError",
    "hydrate_run_transcript_seed",
    "hydrate_transcript_projection_materialization",
]
