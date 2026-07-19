"""Content-addressed persistent vectors used by provider-input generations."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Iterable, overload

import json
from pydantic import TypeAdapter

from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives.context_source import (
    ContextArtifactReferenceFact,
    LedgerAuthorityHorizonFact,
    LedgerAuthorityHorizonSetNodeReferenceFact,
    LedgerAuthorityHorizonSetReferenceFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    ProviderInputReplayBindingIdentityFact,
    ProviderInputReplayBindingSetReferenceFact,
    ProviderInputUnitMaterializationFact,
    ProviderInputUnitVectorNodeReferenceFact,
    ProviderInputUnitVectorRootReferenceFact,
)


LEAF_MAX_UNITS = 128
INTERNAL_MAX_CHILDREN = 64
MAX_TREE_HEIGHT = 8
APPEND_MAX_UNITS = 512
MAX_CHANGED_LEAVES = 5
MAX_CHANGED_NODES = 41
VECTOR_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-persistent-vector-contract:v2",
    {
        "leaf_max_units": LEAF_MAX_UNITS,
        "internal_max_children": INTERNAL_MAX_CHILDREN,
        "max_tree_height": MAX_TREE_HEIGHT,
        "append_max_units": APPEND_MAX_UNITS,
        "max_changed_leaves": MAX_CHANGED_LEAVES,
        "max_changed_nodes": MAX_CHANGED_NODES,
        "ordinal": "unsigned-64-bit-zero-based:v1",
        "append_algorithm": "right-spine-path-copy:v1",
        "ordered_accumulator": "canonical-merkle-subtree:v1",
    },
)
HORIZON_SET_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-ledger-horizon-set-contract:v1",
    {"leaf_max": 64, "internal_max": 64, "ordering": "runtime-session-id"},
)
REPLAY_BINDING_SET_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-replay-binding-set-contract:v1",
    {"ordering": "identity-fingerprint", "fanout": 64},
)


@dataclass(frozen=True, slots=True)
class PreparedProviderInputArtifact:
    artifact_reference: ContextArtifactReferenceFact
    canonical_text: str
    semantic_metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class PreparedProviderInputVector:
    units: "PersistentProviderInputUnitSequence"
    root_reference: ProviderInputUnitVectorRootReferenceFact
    changed_node_references: tuple[ProviderInputUnitVectorNodeReferenceFact, ...]
    artifacts: tuple[PreparedProviderInputArtifact, ...]
    state: "ProviderInputVectorState"


@dataclass(frozen=True, slots=True)
class ProviderInputVectorState:
    units: "PersistentProviderInputUnitSequence"
    levels: tuple[tuple[ProviderInputUnitVectorNodeReferenceFact, ...], ...]
    root_reference: ProviderInputUnitVectorRootReferenceFact

    def __post_init__(self) -> None:
        if not self.units:
            if self.levels or self.root_reference.root_node_ref is not None:
                raise ValueError("empty provider vector state has materialized nodes")
            return
        if not self.levels or len(self.levels[-1]) != 1:
            raise ValueError("provider vector state lacks one root level")
        if self.levels[-1][0] != self.root_reference.root_node_ref:
            raise ValueError("provider vector state root identity drifted")


@dataclass(frozen=True, slots=True)
class PersistentProviderInputUnitSequence(
    Sequence[ProviderInputUnitMaterializationFact]
):
    """Process-local immutable unit sequence with append-time chunk sharing."""

    chunks: tuple[tuple[ProviderInputUnitMaterializationFact, ...], ...]
    chunk_end_offsets: tuple[int, ...]
    unit_count: int

    def __post_init__(self) -> None:
        if self.unit_count < 0:
            raise ValueError("provider unit sequence count is invalid")
        if len(self.chunks) != len(self.chunk_end_offsets):
            raise ValueError("provider unit sequence chunk index drifted")
        if any(not chunk for chunk in self.chunks):
            raise ValueError("provider unit sequence contains an empty chunk")
        expected_ends: list[int] = []
        count = 0
        for chunk in self.chunks:
            count += len(chunk)
            expected_ends.append(count)
        if count != self.unit_count or tuple(expected_ends) != self.chunk_end_offsets:
            raise ValueError("provider unit sequence count/index mismatch")

    @classmethod
    def from_units(
        cls,
        units: Iterable[ProviderInputUnitMaterializationFact],
    ) -> "PersistentProviderInputUnitSequence":
        values = tuple(units)
        chunks = tuple(
            values[offset : offset + LEAF_MAX_UNITS]
            for offset in range(0, len(values), LEAF_MAX_UNITS)
        )
        return cls._from_chunks(chunks)

    @classmethod
    def _from_chunks(
        cls,
        chunks: tuple[tuple[ProviderInputUnitMaterializationFact, ...], ...],
    ) -> "PersistentProviderInputUnitSequence":
        count = 0
        ends: list[int] = []
        for chunk in chunks:
            count += len(chunk)
            ends.append(count)
        return cls(chunks=chunks, chunk_end_offsets=tuple(ends), unit_count=count)

    def append(
        self,
        units: tuple[ProviderInputUnitMaterializationFact, ...],
    ) -> "PersistentProviderInputUnitSequence":
        if not units:
            return self
        appended_chunks = tuple(
            units[offset : offset + LEAF_MAX_UNITS]
            for offset in range(0, len(units), LEAF_MAX_UNITS)
        )
        count = self.unit_count
        new_ends: list[int] = []
        for chunk in appended_chunks:
            count += len(chunk)
            new_ends.append(count)
        return PersistentProviderInputUnitSequence(
            chunks=(*self.chunks, *appended_chunks),
            chunk_end_offsets=(*self.chunk_end_offsets, *new_ends),
            unit_count=count,
        )

    def __len__(self) -> int:
        return self.unit_count

    def __iter__(self) -> Iterator[ProviderInputUnitMaterializationFact]:
        return (item for chunk in self.chunks for item in chunk)

    @overload
    def __getitem__(self, index: int) -> ProviderInputUnitMaterializationFact: ...

    @overload
    def __getitem__(
        self, index: slice
    ) -> tuple[ProviderInputUnitMaterializationFact, ...]: ...

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.unit_count)
            return tuple(self[position] for position in range(start, stop, step))
        position = index
        if position < 0:
            position += self.unit_count
        if position < 0 or position >= self.unit_count:
            raise IndexError("provider unit index is out of range")
        chunk_index = bisect_right(self.chunk_end_offsets, position)
        chunk_start = self.chunk_end_offsets[chunk_index - 1] if chunk_index else 0
        return self.chunks[chunk_index][position - chunk_start]


@dataclass(frozen=True, slots=True)
class PreparedHorizonSet:
    reference: LedgerAuthorityHorizonSetReferenceFact
    artifacts: tuple[PreparedProviderInputArtifact, ...]


@dataclass(frozen=True, slots=True)
class PreparedReplayBindingSet:
    reference: ProviderInputReplayBindingSetReferenceFact
    artifacts: tuple[PreparedProviderInputArtifact, ...]


def prepare_provider_input_vector(
    units: tuple[ProviderInputUnitMaterializationFact, ...],
    *,
    previously_reachable_artifact_ids: frozenset[str] = frozenset(),
    artifact_namespace: str = "standalone",
) -> PreparedProviderInputVector:
    """Build one immutable vector; unchanged content-addressed nodes are reused."""

    if len(units) >= 2**64:
        raise ValueError("provider input vector exceeds fixed ordinal range")
    if not units:
        empty_accumulator = context_fingerprint(
            "provider-input-vector-empty-accumulator:v2", ()
        )
        root = build_frozen_fact(
            ProviderInputUnitVectorRootReferenceFact,
            schema_version="provider_input_unit_vector_root_reference.v1",
            unit_count=0,
            tree_height=0,
            root_node_ref=None,
            ordered_unit_accumulator=empty_accumulator,
            vector_contract_fingerprint=VECTOR_CONTRACT_FINGERPRINT,
            vector_semantic_fingerprint=context_fingerprint(
                "provider-input-unit-vector-semantic:v2",
                {
                    "unit_count": 0,
                    "ordered_unit_accumulator": empty_accumulator,
                },
            ),
        )
        empty_units = PersistentProviderInputUnitSequence.from_units(())
        state = ProviderInputVectorState(empty_units, (), root)
        return PreparedProviderInputVector(empty_units, root, (), (), state)

    artifacts: list[PreparedProviderInputArtifact] = []
    node_references: list[ProviderInputUnitVectorNodeReferenceFact] = []
    levels: list[tuple[ProviderInputUnitVectorNodeReferenceFact, ...]] = []
    current: list[ProviderInputUnitVectorNodeReferenceFact] = []
    for first in range(0, len(units), LEAF_MAX_UNITS):
        leaf_reference, artifact = _build_leaf_node(
            units=units[first : first + LEAF_MAX_UNITS],
            first_ordinal=first,
            artifact_namespace=artifact_namespace,
        )
        current.append(leaf_reference)
        node_references.append(leaf_reference)
        artifacts.append(artifact)
    levels.append(tuple(current))

    height = 1
    while len(current) > 1:
        height += 1
        if height > MAX_TREE_HEIGHT:
            raise ValueError("provider input vector exceeds maximum tree height")
        next_level: list[ProviderInputUnitVectorNodeReferenceFact] = []
        for offset in range(0, len(current), INTERNAL_MAX_CHILDREN):
            children = tuple(current[offset : offset + INTERNAL_MAX_CHILDREN])
            internal_reference, artifact = _build_internal_node(
                children=children,
                height=height,
                artifact_namespace=artifact_namespace,
            )
            next_level.append(internal_reference)
            node_references.append(internal_reference)
            artifacts.append(artifact)
        current = next_level
        levels.append(tuple(current))

    root_node = current[0]
    root = _vector_root(root_node=root_node, unit_count=len(units))
    new_artifacts = tuple(
        item
        for item in _unique_artifacts(artifacts)
        if item.artifact_reference.artifact_id not in previously_reachable_artifact_ids
    )
    changed_refs_by_artifact = {
        item.artifact_reference.artifact_id: item for item in node_references
    }
    changed = tuple(
        changed_refs_by_artifact[key]
        for key in sorted(changed_refs_by_artifact)
        if key not in previously_reachable_artifact_ids
    )
    if previously_reachable_artifact_ids and len(changed) > MAX_CHANGED_NODES:
        raise ValueError("provider input append exceeds changed-node bound")
    unit_sequence = PersistentProviderInputUnitSequence.from_units(units)
    state = ProviderInputVectorState(unit_sequence, tuple(levels), root)
    return PreparedProviderInputVector(
        unit_sequence, root, changed, new_artifacts, state
    )


def append_provider_input_vector(
    previous: ProviderInputVectorState,
    append_units: tuple[ProviderInputUnitMaterializationFact, ...],
    *,
    artifact_namespace: str = "standalone",
) -> PreparedProviderInputVector:
    """Append with deterministic right-spine path copying."""

    if not append_units:
        raise ValueError("provider input vector append is empty")
    if len(append_units) > APPEND_MAX_UNITS:
        raise ValueError("provider input vector append exceeds hard bound")
    if len(previous.units) + len(append_units) >= 2**64:
        raise ValueError("provider input vector exceeds fixed ordinal range")
    if not previous.units:
        return prepare_provider_input_vector(
            append_units,
            artifact_namespace=artifact_namespace,
        )

    artifacts: list[PreparedProviderInputArtifact] = []
    changed_refs: list[ProviderInputUnitVectorNodeReferenceFact] = []
    old_leaves = previous.levels[0]
    old_tail_count = old_leaves[-1].subtree_unit_count
    if old_tail_count < LEAF_MAX_UNITS:
        first_changed_leaf = len(old_leaves) - 1
        changed_units = (
            *previous.units[-old_tail_count:],
            *append_units,
        )
        first_ordinal = len(previous.units) - old_tail_count
    else:
        first_changed_leaf = len(old_leaves)
        changed_units = append_units
        first_ordinal = len(previous.units)

    new_leaves = list(old_leaves[:first_changed_leaf])
    for offset in range(0, len(changed_units), LEAF_MAX_UNITS):
        leaf, artifact = _build_leaf_node(
            units=tuple(changed_units[offset : offset + LEAF_MAX_UNITS]),
            first_ordinal=first_ordinal + offset,
            artifact_namespace=artifact_namespace,
        )
        new_leaves.append(leaf)
        changed_refs.append(leaf)
        artifacts.append(artifact)
    if len(changed_refs) > MAX_CHANGED_LEAVES:
        raise ValueError("provider input append exceeds changed-leaf bound")

    levels: list[tuple[ProviderInputUnitVectorNodeReferenceFact, ...]] = [
        tuple(new_leaves)
    ]
    current = tuple(new_leaves)
    first_changed_child = first_changed_leaf
    height = 1
    while len(current) > 1:
        height += 1
        if height > MAX_TREE_HEIGHT:
            raise ValueError("provider input vector exceeds maximum tree height")
        old_level = (
            previous.levels[height - 1] if height - 1 < len(previous.levels) else ()
        )
        first_changed_parent = first_changed_child // INTERNAL_MAX_CHILDREN
        prefix = list(old_level[:first_changed_parent])
        rebuilt: list[ProviderInputUnitVectorNodeReferenceFact] = []
        for offset in range(
            first_changed_parent * INTERNAL_MAX_CHILDREN,
            len(current),
            INTERNAL_MAX_CHILDREN,
        ):
            node, artifact = _build_internal_node(
                children=tuple(current[offset : offset + INTERNAL_MAX_CHILDREN]),
                height=height,
                artifact_namespace=artifact_namespace,
            )
            rebuilt.append(node)
            changed_refs.append(node)
            artifacts.append(artifact)
        current = tuple((*prefix, *rebuilt))
        levels.append(current)
        first_changed_child = first_changed_parent

    if len(changed_refs) > MAX_CHANGED_NODES:
        raise ValueError("provider input append exceeds changed-node bound")
    all_units = previous.units.append(append_units)
    root = _vector_root(root_node=current[0], unit_count=len(all_units))
    state = ProviderInputVectorState(all_units, tuple(levels), root)
    return PreparedProviderInputVector(
        all_units,
        root,
        tuple(changed_refs),
        _unique_artifacts(artifacts),
        state,
    )


def prepare_ledger_horizon_set(
    horizons: Iterable[LedgerAuthorityHorizonFact],
    *,
    artifact_namespace: str = "standalone",
) -> PreparedHorizonSet:
    ordered = tuple(sorted(horizons, key=lambda item: item.runtime_session_id))
    owners = tuple(item.runtime_session_id for item in ordered)
    if owners != tuple(sorted(set(owners))):
        raise ValueError("provider authority horizons are not unique")
    accumulator = context_fingerprint(
        "provider-input-ledger-horizon-set-accumulator:v1",
        tuple(item.horizon_fingerprint for item in ordered),
    )
    if not ordered:
        return PreparedHorizonSet(
            build_frozen_fact(
                LedgerAuthorityHorizonSetReferenceFact,
                schema_version="ledger_authority_horizon_set_reference.v1",
                horizon_count=0,
                ordered_horizon_accumulator=accumulator,
                root_node_ref=None,
                set_contract_fingerprint=HORIZON_SET_CONTRACT_FINGERPRINT,
            ),
            (),
        )
    artifacts: list[PreparedProviderInputArtifact] = []
    current: list[
        tuple[
            LedgerAuthorityHorizonSetNodeReferenceFact,
            tuple[LedgerAuthorityHorizonFact, ...],
        ]
    ] = []
    for offset in range(0, len(ordered), 64):
        leaf_horizons = ordered[offset : offset + 64]
        leaf_accumulator = context_fingerprint(
            "provider-input-ledger-horizon-set-accumulator:v1",
            tuple(item.horizon_fingerprint for item in leaf_horizons),
        )
        artifact = _artifact(
            "provider-input-horizon-set",
            {
                "schema_version": "provider_input_ledger_horizon_set_leaf.v1",
                "horizons": tuple(
                    item.model_dump(mode="json") for item in leaf_horizons
                ),
            },
            artifact_namespace=artifact_namespace,
            contract_fingerprint=HORIZON_SET_CONTRACT_FINGERPRINT,
            metadata_kind="provider_input_horizon_set_leaf",
        )
        artifacts.append(artifact)
        current.append(
            (
                build_frozen_fact(
                    LedgerAuthorityHorizonSetNodeReferenceFact,
                    schema_version=("ledger_authority_horizon_set_node_reference.v1"),
                    node_kind="leaf",
                    first_runtime_session_id=leaf_horizons[0].runtime_session_id,
                    last_runtime_session_id=leaf_horizons[-1].runtime_session_id,
                    subtree_horizon_count=len(leaf_horizons),
                    subtree_horizon_accumulator=leaf_accumulator,
                    artifact_reference=artifact.artifact_reference,
                ),
                leaf_horizons,
            )
        )
    while len(current) > 1:
        next_level = []
        for offset in range(0, len(current), 64):
            children = current[offset : offset + 64]
            child_refs = tuple(item[0] for item in children)
            child_horizons = tuple(horizon for item in children for horizon in item[1])
            child_accumulator = context_fingerprint(
                "provider-input-ledger-horizon-set-accumulator:v1",
                tuple(item.horizon_fingerprint for item in child_horizons),
            )
            artifact = _artifact(
                "provider-input-horizon-set",
                {
                    "schema_version": ("provider_input_ledger_horizon_set_internal.v1"),
                    "children": tuple(
                        item.model_dump(mode="json") for item in child_refs
                    ),
                },
                artifact_namespace=artifact_namespace,
                contract_fingerprint=HORIZON_SET_CONTRACT_FINGERPRINT,
                metadata_kind="provider_input_horizon_set_internal",
            )
            artifacts.append(artifact)
            next_level.append(
                (
                    build_frozen_fact(
                        LedgerAuthorityHorizonSetNodeReferenceFact,
                        schema_version=(
                            "ledger_authority_horizon_set_node_reference.v1"
                        ),
                        node_kind="internal",
                        first_runtime_session_id=(child_horizons[0].runtime_session_id),
                        last_runtime_session_id=(child_horizons[-1].runtime_session_id),
                        subtree_horizon_count=len(child_horizons),
                        subtree_horizon_accumulator=child_accumulator,
                        artifact_reference=artifact.artifact_reference,
                    ),
                    child_horizons,
                )
            )
        current = next_level
    node = current[0][0]
    reference = build_frozen_fact(
        LedgerAuthorityHorizonSetReferenceFact,
        schema_version="ledger_authority_horizon_set_reference.v1",
        horizon_count=len(ordered),
        ordered_horizon_accumulator=accumulator,
        root_node_ref=node,
        set_contract_fingerprint=HORIZON_SET_CONTRACT_FINGERPRINT,
    )
    return PreparedHorizonSet(reference, _unique_artifacts(artifacts))


def prepare_replay_binding_set(
    bindings: Iterable[ProviderInputReplayBindingIdentityFact],
    *,
    artifact_namespace: str = "standalone",
) -> PreparedReplayBindingSet:
    ordered = tuple(sorted(bindings, key=lambda item: item.identity_fingerprint))
    identities = tuple(item.identity_fingerprint for item in ordered)
    if identities != tuple(sorted(set(identities))):
        raise ValueError("provider replay bindings are not unique")
    accumulator = context_fingerprint(
        "provider-input-replay-binding-set-accumulator:v1", identities
    )
    if not ordered:
        return PreparedReplayBindingSet(
            build_frozen_fact(
                ProviderInputReplayBindingSetReferenceFact,
                schema_version="provider_input_replay_binding_set_reference.v1",
                binding_count=0,
                ordered_binding_accumulator=accumulator,
                root_artifact_ref=None,
                set_contract_fingerprint=REPLAY_BINDING_SET_CONTRACT_FINGERPRINT,
            ),
            (),
        )
    artifacts: list[PreparedProviderInputArtifact] = []
    current: list[
        tuple[
            PreparedProviderInputArtifact,
            tuple[ProviderInputReplayBindingIdentityFact, ...],
        ]
    ] = []
    for offset in range(0, len(ordered), 64):
        leaf_bindings = ordered[offset : offset + 64]
        artifact = _artifact(
            "provider-input-replay-binding-set",
            {
                "schema_version": "provider_input_replay_binding_set_leaf.v1",
                "bindings": tuple(
                    item.model_dump(mode="json") for item in leaf_bindings
                ),
            },
            artifact_namespace=artifact_namespace,
            contract_fingerprint=REPLAY_BINDING_SET_CONTRACT_FINGERPRINT,
            metadata_kind="provider_input_replay_binding_set_leaf",
        )
        artifacts.append(artifact)
        current.append((artifact, leaf_bindings))
    while len(current) > 1:
        next_level = []
        for offset in range(0, len(current), 64):
            children = current[offset : offset + 64]
            child_bindings = tuple(binding for item in children for binding in item[1])
            artifact = _artifact(
                "provider-input-replay-binding-set",
                {
                    "schema_version": ("provider_input_replay_binding_set_internal.v1"),
                    "children": tuple(
                        item[0].artifact_reference.model_dump(mode="json")
                        for item in children
                    ),
                },
                artifact_namespace=artifact_namespace,
                contract_fingerprint=REPLAY_BINDING_SET_CONTRACT_FINGERPRINT,
                metadata_kind="provider_input_replay_binding_set_internal",
            )
            artifacts.append(artifact)
            next_level.append((artifact, child_bindings))
        current = next_level
    root_artifact = current[0][0]
    reference = build_frozen_fact(
        ProviderInputReplayBindingSetReferenceFact,
        schema_version="provider_input_replay_binding_set_reference.v1",
        binding_count=len(ordered),
        ordered_binding_accumulator=accumulator,
        root_artifact_ref=root_artifact.artifact_reference,
        set_contract_fingerprint=REPLAY_BINDING_SET_CONTRACT_FINGERPRINT,
    )
    return PreparedReplayBindingSet(reference, _unique_artifacts(artifacts))


async def persist_provider_input_artifacts(
    *,
    runtime_session,
    run_id: str | None,
    artifacts: Iterable[PreparedProviderInputArtifact],
    deadline_monotonic: float | None = None,
) -> None:
    deadline = deadline_monotonic or monotonic() + 30.0
    for artifact in _unique_artifacts(tuple(artifacts)):
        reference = artifact.artifact_reference

        def write_and_confirm(
            artifact: PreparedProviderInputArtifact = artifact,
            reference: ContextArtifactReferenceFact = reference,
        ) -> None:
            confirmation = (
                runtime_session.archive.put_text_if_absent_or_confirm_identical(
                    reference.artifact_id,
                    artifact.canonical_text,
                    session_id=runtime_session.runtime_session_id,
                    # Generation artifacts are content-addressed and may be reused by
                    # a later run in the same runtime ledger. Run ownership would make
                    # identical content conflict on that legitimate reuse.
                    run_id=None,
                    media_type=reference.media_type,
                    semantic_metadata=artifact.semantic_metadata,
                    deadline_monotonic=deadline,
                )
            )
            if (
                confirmation.result.id != reference.artifact_id
                or confirmation.result.digest != reference.content_sha256
                or confirmation.result.size_bytes != reference.content_bytes
            ):
                raise ValueError("provider input artifact confirmation drifted")

        await runtime_session.context_input_io_service.execute(
            operation_name=f"provider-input-artifact:{reference.artifact_id}",
            operation=write_and_confirm,
            deadline_monotonic=deadline,
        )


def load_provider_input_vector_state(
    *,
    archive,
    runtime_session_id: str,
    root: ProviderInputUnitVectorRootReferenceFact,
    deadline_monotonic: float,
) -> tuple[ProviderInputVectorState, frozenset[str]]:
    """Hydrate and verify a persistent vector from its bounded root."""

    if root.root_node_ref is None:
        expected = prepare_provider_input_vector(()).state
        if expected.root_reference != root:
            raise ValueError("empty provider vector root drifted")
        return expected, frozenset()
    reachable: set[str] = set()
    levels_by_height: dict[int, list[ProviderInputUnitVectorNodeReferenceFact]] = {}

    def load_node(
        reference: ProviderInputUnitVectorNodeReferenceFact,
    ) -> tuple[ProviderInputUnitMaterializationFact, ...]:
        levels_by_height.setdefault(reference.height, []).append(reference)
        artifact = reference.artifact_reference
        text = archive.get_text(
            artifact.artifact_id,
            session_id=runtime_session_id,
            deadline_monotonic=deadline_monotonic,
        )
        encoded = text.encode("utf-8")
        if (
            len(encoded) != artifact.content_bytes
            or f"sha256:{sha256(encoded).hexdigest()}" != artifact.content_sha256
        ):
            raise ValueError("provider vector artifact content drifted")
        reachable.add(artifact.artifact_id)
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("provider vector node artifact is not an object")
        if reference.node_kind == "leaf":
            if payload.get("schema_version") != "provider_input_unit_vector_leaf.v2":
                raise ValueError("provider vector leaf schema drifted")
            if payload.get("first_ordinal") != reference.first_ordinal:
                raise ValueError("provider vector leaf ordinal drifted")
            units = TypeAdapter(
                tuple[ProviderInputUnitMaterializationFact, ...]
            ).validate_python(payload.get("units"))
            expected_accumulator = _leaf_accumulator(units)
        else:
            if (
                payload.get("schema_version")
                != "provider_input_unit_vector_internal.v2"
            ):
                raise ValueError("provider vector internal schema drifted")
            if payload.get("height") != reference.height:
                raise ValueError("provider vector internal height drifted")
            children = TypeAdapter(
                tuple[ProviderInputUnitVectorNodeReferenceFact, ...]
            ).validate_python(payload.get("children"))
            if not children or tuple(item.height for item in children) != tuple(
                reference.height - 1 for _ in children
            ):
                raise ValueError("provider vector child height drifted")
            units = tuple(item for child in children for item in load_node(child))
            expected_accumulator = _internal_accumulator(
                height=reference.height,
                children=children,
            )
        if (
            len(units) != reference.subtree_unit_count
            or reference.last_ordinal - reference.first_ordinal + 1 != len(units)
            or reference.subtree_accumulator != expected_accumulator
        ):
            raise ValueError("provider vector hydrated range drifted")
        return units

    units = load_node(root.root_node_ref)
    if (
        len(units) != root.unit_count
        or root.root_node_ref.subtree_accumulator != root.ordered_unit_accumulator
    ):
        raise ValueError("provider vector root hydration drifted")
    if (
        context_fingerprint(
            "provider-input-unit-vector-semantic:v2",
            {
                "unit_count": len(units),
                "ordered_unit_accumulator": root.ordered_unit_accumulator,
            },
        )
        != root.vector_semantic_fingerprint
    ):
        raise ValueError("provider vector semantic fingerprint drifted")
    levels = tuple(
        tuple(levels_by_height[height]) for height in range(1, root.tree_height + 1)
    )
    unit_sequence = PersistentProviderInputUnitSequence.from_units(units)
    state = ProviderInputVectorState(unit_sequence, levels, root)
    return state, frozenset(reachable)


def load_provider_input_vector(
    *,
    archive,
    runtime_session_id: str,
    root: ProviderInputUnitVectorRootReferenceFact,
    deadline_monotonic: float,
) -> tuple[
    tuple[ProviderInputUnitMaterializationFact, ...],
    frozenset[str],
]:
    """Compatibility wrapper for callers that only need hydrated units."""

    state, reachable = load_provider_input_vector_state(
        archive=archive,
        runtime_session_id=runtime_session_id,
        root=root,
        deadline_monotonic=deadline_monotonic,
    )
    return tuple(state.units), reachable


def load_ledger_horizon_set(
    *,
    archive,
    runtime_session_id: str,
    reference: LedgerAuthorityHorizonSetReferenceFact,
    deadline_monotonic: float,
) -> tuple[tuple[LedgerAuthorityHorizonFact, ...], frozenset[str]]:
    """Hydrate and verify one bounded persistent authority-horizon set."""

    if reference.root_node_ref is None:
        prepared = prepare_ledger_horizon_set(())
        if prepared.reference != reference:
            raise ValueError("empty provider horizon-set reference drifted")
        return (), frozenset()
    reachable: set[str] = set()

    def load_node(
        node: LedgerAuthorityHorizonSetNodeReferenceFact,
    ) -> tuple[LedgerAuthorityHorizonFact, ...]:
        payload = _read_artifact_json(
            archive=archive,
            runtime_session_id=runtime_session_id,
            reference=node.artifact_reference,
            deadline_monotonic=deadline_monotonic,
            reachable=reachable,
        )
        schema_version = payload.get("schema_version")
        if node.node_kind == "leaf":
            if schema_version != "provider_input_ledger_horizon_set_leaf.v1":
                raise ValueError("provider horizon-set leaf schema drifted")
            horizons = TypeAdapter(
                tuple[LedgerAuthorityHorizonFact, ...]
            ).validate_python(payload.get("horizons"))
        else:
            if schema_version != "provider_input_ledger_horizon_set_internal.v1":
                raise ValueError("provider horizon-set internal schema drifted")
            children = TypeAdapter(
                tuple[LedgerAuthorityHorizonSetNodeReferenceFact, ...]
            ).validate_python(payload.get("children"))
            if not children:
                raise ValueError("provider horizon-set internal node is empty")
            horizons = tuple(
                horizon for child in children for horizon in load_node(child)
            )
        accumulator = context_fingerprint(
            "provider-input-ledger-horizon-set-accumulator:v1",
            tuple(item.horizon_fingerprint for item in horizons),
        )
        if (
            len(horizons) != node.subtree_horizon_count
            or accumulator != node.subtree_horizon_accumulator
            or not horizons
            or horizons[0].runtime_session_id != node.first_runtime_session_id
            or horizons[-1].runtime_session_id != node.last_runtime_session_id
        ):
            raise ValueError("provider horizon-set node hydration drifted")
        return horizons

    horizons = load_node(reference.root_node_ref)
    if (
        prepare_ledger_horizon_set(
            horizons,
            artifact_namespace=_artifact_namespace_from_id(
                reference.root_node_ref.artifact_reference.artifact_id,
                expected_family="provider-input-horizon-set",
            ),
        ).reference
        != reference
    ):
        raise ValueError("provider horizon-set root hydration drifted")
    return horizons, frozenset(reachable)


def load_replay_binding_set(
    *,
    archive,
    runtime_session_id: str,
    reference: ProviderInputReplayBindingSetReferenceFact,
    deadline_monotonic: float,
) -> tuple[
    tuple[ProviderInputReplayBindingIdentityFact, ...],
    frozenset[str],
]:
    """Hydrate and verify one bounded historical-binding set."""

    if reference.root_artifact_ref is None:
        prepared = prepare_replay_binding_set(())
        if prepared.reference != reference:
            raise ValueError("empty provider replay-binding reference drifted")
        return (), frozenset()
    reachable: set[str] = set()

    def load_artifact(
        artifact_reference: ContextArtifactReferenceFact,
    ) -> tuple[ProviderInputReplayBindingIdentityFact, ...]:
        payload = _read_artifact_json(
            archive=archive,
            runtime_session_id=runtime_session_id,
            reference=artifact_reference,
            deadline_monotonic=deadline_monotonic,
            reachable=reachable,
        )
        schema_version = payload.get("schema_version")
        if schema_version == "provider_input_replay_binding_set_leaf.v1":
            return TypeAdapter(
                tuple[ProviderInputReplayBindingIdentityFact, ...]
            ).validate_python(payload.get("bindings"))
        if schema_version != "provider_input_replay_binding_set_internal.v1":
            raise ValueError("provider replay-binding node schema drifted")
        children = TypeAdapter(
            tuple[ContextArtifactReferenceFact, ...]
        ).validate_python(payload.get("children"))
        if not children:
            raise ValueError("provider replay-binding internal node is empty")
        return tuple(binding for child in children for binding in load_artifact(child))

    bindings = load_artifact(reference.root_artifact_ref)
    if (
        prepare_replay_binding_set(
            bindings,
            artifact_namespace=_artifact_namespace_from_id(
                reference.root_artifact_ref.artifact_id,
                expected_family="provider-input-replay-binding-set",
            ),
        ).reference
        != reference
    ):
        raise ValueError("provider replay-binding root hydration drifted")
    return bindings, frozenset(reachable)


def _read_artifact_json(
    *,
    archive,
    runtime_session_id: str,
    reference: ContextArtifactReferenceFact,
    deadline_monotonic: float,
    reachable: set[str],
) -> dict[str, object]:
    text = archive.get_text(
        reference.artifact_id,
        session_id=runtime_session_id,
        deadline_monotonic=deadline_monotonic,
    )
    encoded = text.encode("utf-8")
    if (
        len(encoded) != reference.content_bytes
        or f"sha256:{sha256(encoded).hexdigest()}" != reference.content_sha256
    ):
        raise ValueError("provider input artifact content drifted")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("provider input artifact is not an object")
    reachable.add(reference.artifact_id)
    return payload


def _leaf_accumulator(
    units: tuple[ProviderInputUnitMaterializationFact, ...],
) -> str:
    return context_fingerprint(
        "provider-input-vector-leaf-accumulator:v2",
        tuple(item.attribution.semantic.semantic_fingerprint for item in units),
    )


def _internal_accumulator(
    *,
    height: int,
    children: tuple[ProviderInputUnitVectorNodeReferenceFact, ...],
) -> str:
    return context_fingerprint(
        "provider-input-vector-internal-accumulator:v2",
        {
            "height": height,
            "children": tuple(
                {
                    "subtree_unit_count": item.subtree_unit_count,
                    "subtree_accumulator": item.subtree_accumulator,
                }
                for item in children
            ),
        },
    )


def _build_leaf_node(
    *,
    units: tuple[ProviderInputUnitMaterializationFact, ...],
    first_ordinal: int,
    artifact_namespace: str,
) -> tuple[
    ProviderInputUnitVectorNodeReferenceFact,
    PreparedProviderInputArtifact,
]:
    if not units or len(units) > LEAF_MAX_UNITS:
        raise ValueError("provider vector leaf unit bound violated")
    artifact = _artifact(
        "provider-input-vector-node",
        {
            "schema_version": "provider_input_unit_vector_leaf.v2",
            "first_ordinal": first_ordinal,
            "units": tuple(item.model_dump(mode="json") for item in units),
        },
        artifact_namespace=artifact_namespace,
        contract_fingerprint=VECTOR_CONTRACT_FINGERPRINT,
        metadata_kind="provider_input_vector_leaf",
    )
    reference = build_frozen_fact(
        ProviderInputUnitVectorNodeReferenceFact,
        schema_version="provider_input_unit_vector_node_reference.v1",
        node_kind="leaf",
        height=1,
        first_ordinal=first_ordinal,
        last_ordinal=first_ordinal + len(units) - 1,
        subtree_unit_count=len(units),
        subtree_accumulator=_leaf_accumulator(units),
        artifact_reference=artifact.artifact_reference,
    )
    return reference, artifact


def _build_internal_node(
    *,
    children: tuple[ProviderInputUnitVectorNodeReferenceFact, ...],
    height: int,
    artifact_namespace: str,
) -> tuple[
    ProviderInputUnitVectorNodeReferenceFact,
    PreparedProviderInputArtifact,
]:
    if not children or len(children) > INTERNAL_MAX_CHILDREN:
        raise ValueError("provider vector internal fanout bound violated")
    if any(item.height != height - 1 for item in children):
        raise ValueError("provider vector internal child height drifted")
    artifact = _artifact(
        "provider-input-vector-node",
        {
            "schema_version": "provider_input_unit_vector_internal.v2",
            "height": height,
            "children": tuple(item.model_dump(mode="json") for item in children),
        },
        artifact_namespace=artifact_namespace,
        contract_fingerprint=VECTOR_CONTRACT_FINGERPRINT,
        metadata_kind="provider_input_vector_internal",
    )
    reference = build_frozen_fact(
        ProviderInputUnitVectorNodeReferenceFact,
        schema_version="provider_input_unit_vector_node_reference.v1",
        node_kind="internal",
        height=height,
        first_ordinal=children[0].first_ordinal,
        last_ordinal=children[-1].last_ordinal,
        subtree_unit_count=sum(item.subtree_unit_count for item in children),
        subtree_accumulator=_internal_accumulator(
            height=height,
            children=children,
        ),
        artifact_reference=artifact.artifact_reference,
    )
    return reference, artifact


def _vector_root(
    *,
    root_node: ProviderInputUnitVectorNodeReferenceFact,
    unit_count: int,
) -> ProviderInputUnitVectorRootReferenceFact:
    return build_frozen_fact(
        ProviderInputUnitVectorRootReferenceFact,
        schema_version="provider_input_unit_vector_root_reference.v1",
        unit_count=unit_count,
        tree_height=root_node.height,
        root_node_ref=root_node,
        ordered_unit_accumulator=root_node.subtree_accumulator,
        vector_contract_fingerprint=VECTOR_CONTRACT_FINGERPRINT,
        vector_semantic_fingerprint=context_fingerprint(
            "provider-input-unit-vector-semantic:v2",
            {
                "unit_count": unit_count,
                "ordered_unit_accumulator": root_node.subtree_accumulator,
            },
        ),
    )


def _artifact(
    namespace: str,
    payload: object,
    *,
    artifact_namespace: str,
    contract_fingerprint: str,
    metadata_kind: str,
) -> PreparedProviderInputArtifact:
    encoded = canonical_json_bytes(payload)
    digest = f"sha256:{sha256(encoded).hexdigest()}"
    artifact_id = f"{namespace}:{artifact_namespace}:{digest.removeprefix('sha256:')}"
    reference = build_frozen_fact(
        ContextArtifactReferenceFact,
        schema_version="context_artifact_reference.v1",
        artifact_id=artifact_id,
        media_type="application/json; charset=utf-8",
        content_sha256=digest,
        content_bytes=len(encoded),
        artifact_contract_fingerprint=contract_fingerprint,
    )
    return PreparedProviderInputArtifact(
        artifact_reference=reference,
        canonical_text=encoded.decode("utf-8"),
        semantic_metadata={
            "artifact_kind": metadata_kind,
            "artifact_contract_fingerprint": contract_fingerprint,
        },
    )


def prepared_json_artifact(
    namespace: str,
    payload: object,
    *,
    artifact_namespace: str = "standalone",
    contract_fingerprint: str,
    metadata_kind: str,
) -> PreparedProviderInputArtifact:
    return _artifact(
        namespace,
        payload,
        artifact_namespace=artifact_namespace,
        contract_fingerprint=contract_fingerprint,
        metadata_kind=metadata_kind,
    )


def provider_input_artifact_namespace(runtime_session_id: str) -> str:
    """Return the physical namespace for one ledger-owned artifact family."""

    return sha256(runtime_session_id.encode("utf-8")).hexdigest()[:16]


def _artifact_namespace_from_id(
    artifact_id: str,
    *,
    expected_family: str,
) -> str:
    parts = artifact_id.split(":")
    if len(parts) != 3 or parts[0] != expected_family or not parts[1]:
        raise ValueError("provider input artifact namespace is malformed")
    return parts[1]


def _unique_artifacts(
    artifacts: Iterable[PreparedProviderInputArtifact],
) -> tuple[PreparedProviderInputArtifact, ...]:
    by_id: dict[str, PreparedProviderInputArtifact] = {}
    for artifact in artifacts:
        existing = by_id.get(artifact.artifact_reference.artifact_id)
        if existing is not None and existing != artifact:
            raise ValueError("content-addressed provider artifact identity conflict")
        by_id[artifact.artifact_reference.artifact_id] = artifact
    return tuple(by_id[key] for key in sorted(by_id))


__all__ = [
    "APPEND_MAX_UNITS",
    "MAX_CHANGED_NODES",
    "PreparedProviderInputArtifact",
    "PreparedProviderInputVector",
    "ProviderInputVectorState",
    "VECTOR_CONTRACT_FINGERPRINT",
    "append_provider_input_vector",
    "persist_provider_input_artifacts",
    "load_ledger_horizon_set",
    "load_provider_input_vector",
    "load_provider_input_vector_state",
    "load_replay_binding_set",
    "prepare_ledger_horizon_set",
    "prepare_provider_input_vector",
    "prepare_replay_binding_set",
    "prepared_json_artifact",
    "provider_input_artifact_namespace",
]
