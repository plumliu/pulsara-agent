"""Content-addressed path-copy tree for unified terminal history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic

from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives._context_base import canonical_json_bytes
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.presentation_history import (
    PresentationHistoryEntryFact,
    PresentationHistoryInternalNodeFact,
    PresentationHistoryLeafNodeFact,
    PresentationHistoryTailMutationFact,
    PresentationHistoryTreeContractFact,
    PresentationHistoryTreeNodeReferenceFact,
    RemovePresentationHistoryEntryMutationFact,
    UpsertPresentationHistoryEntryMutationFact,
)


PRESENTATION_HISTORY_NODE_MEDIA_TYPE = (
    "application/vnd.pulsara.presentation-history-node+json;version=1"
)


@dataclass(frozen=True, slots=True)
class PreparedPresentationHistoryArtifact:
    artifact_id: str
    canonical_bytes: bytes
    media_type: str
    semantic_metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class PreparedPresentationHistoryTreeUpdate:
    resulting_root_node_reference: PresentationHistoryTreeNodeReferenceFact | None
    resulting_tree_height: int
    resulting_entry_count: int
    resulting_entry_accumulator: str
    newly_prepared_artifacts: tuple[PreparedPresentationHistoryArtifact, ...]
    changed_node_count: int
    changed_node_bytes: int
    update_fingerprint: str


@dataclass(frozen=True, slots=True)
class PresentationHistoryTreePage:
    ordered_ranked_entries: tuple[tuple[PresentationHistoryEntryFact, int], ...]
    has_more: bool
    node_read_count: int


class PresentationHistoryTreeError(RuntimeError):
    pass


class PresentationHistoryPersistentTree:
    """Prepare bounded immutable path-copy updates against ArtifactStore nodes."""

    def __init__(
        self,
        *,
        runtime_session_id: str,
        archive: ArtifactStore,
        contract: PresentationHistoryTreeContractFact,
    ) -> None:
        self.runtime_session_id = runtime_session_id
        self.archive = archive
        self.contract = contract
        self._prepared: dict[str, PreparedPresentationHistoryArtifact] = {}

    def prepare_update(
        self,
        *,
        base_root_node_reference: PresentationHistoryTreeNodeReferenceFact | None,
        base_tree_height: int,
        ordered_mutations: tuple[PresentationHistoryTailMutationFact, ...],
        deadline_monotonic: float | None = None,
    ) -> PreparedPresentationHistoryTreeUpdate:
        if (base_root_node_reference is None) != (base_tree_height == 0):
            raise PresentationHistoryTreeError("history root reference/height mismatch")
        self._prepared = {}
        root = base_root_node_reference
        height = base_tree_height
        for mutation in ordered_mutations:
            if isinstance(mutation, UpsertPresentationHistoryEntryMutationFact):
                roots = self._insert(
                    root,
                    mutation,
                    deadline_monotonic=deadline_monotonic,
                )
            else:
                assert isinstance(mutation, RemovePresentationHistoryEntryMutationFact)
                roots = self._delete(
                    root,
                    mutation,
                    deadline_monotonic=deadline_monotonic,
                )
            if not roots:
                root = None
                height = 0
            elif len(roots) == 1:
                root = roots[0]
                if height == 0:
                    height = 1
            else:
                root = self._build_internal(level=max(height, 1), children=tuple(roots))
                height = max(height, 1) + 1
            root, height = self._collapse_single_child_root(
                root,
                height,
                deadline_monotonic=deadline_monotonic,
            )
            if height > self.contract.max_tree_height:
                raise PresentationHistoryTreeError(
                    "presentation history tree height is exhausted"
                )
            if (
                root is not None
                and root.subtree_entry_count
                > self.contract.maximum_representable_entries
            ):
                raise PresentationHistoryTreeError(
                    "presentation history tree entry capacity is exhausted"
                )
        artifacts = tuple(self._prepared[key] for key in sorted(self._prepared))
        changed_bytes = sum(len(item.canonical_bytes) for item in artifacts)
        result_count = root.subtree_entry_count if root is not None else 0
        result_accumulator = (
            root.subtree_entry_accumulator
            if root is not None
            else context_fingerprint("presentation-history-ordered-entries:v1", ())
        )
        payload = {
            "base_root_reference_fingerprint": (
                base_root_node_reference.node_reference_fingerprint
                if base_root_node_reference is not None
                else None
            ),
            "base_tree_height": base_tree_height,
            "ordered_mutation_fingerprints": tuple(
                item.mutation_fingerprint for item in ordered_mutations
            ),
            "resulting_root_reference_fingerprint": (
                root.node_reference_fingerprint if root is not None else None
            ),
            "resulting_tree_height": height,
            "resulting_entry_count": result_count,
            "resulting_entry_accumulator": result_accumulator,
            "new_artifact_ids": tuple(item.artifact_id for item in artifacts),
        }
        return PreparedPresentationHistoryTreeUpdate(
            resulting_root_node_reference=root,
            resulting_tree_height=height,
            resulting_entry_count=result_count,
            resulting_entry_accumulator=result_accumulator,
            newly_prepared_artifacts=artifacts,
            changed_node_count=len(artifacts),
            changed_node_bytes=changed_bytes,
            update_fingerprint=context_fingerprint(
                "prepared-presentation-history-tree-update:v1", payload
            ),
        )

    def persist_prepared(
        self,
        prepared: PreparedPresentationHistoryTreeUpdate,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        for artifact in prepared.newly_prepared_artifacts:
            self.archive.put_text_if_absent_or_confirm_identical(
                artifact.artifact_id,
                artifact.canonical_bytes.decode("utf-8"),
                session_id=self.runtime_session_id,
                run_id=None,
                media_type=artifact.media_type,
                semantic_metadata=artifact.semantic_metadata,
                deadline_monotonic=deadline_monotonic,
            )

    def read_ordered_entries(
        self,
        root: PresentationHistoryTreeNodeReferenceFact | None,
        *,
        max_entries: int,
        max_node_reads: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[PresentationHistoryEntryFact, ...]:
        if max_entries <= 0 or max_node_reads <= 0:
            raise ValueError("history tree read bounds must be positive")
        reads = [0]
        entries: list[PresentationHistoryEntryFact] = []

        def visit(reference: PresentationHistoryTreeNodeReferenceFact) -> None:
            reads[0] += 1
            if reads[0] > max_node_reads:
                raise PresentationHistoryTreeError(
                    "history tree node-read bound exceeded"
                )
            node = self._load(
                reference,
                deadline_monotonic=deadline_monotonic,
            )
            if isinstance(node, PresentationHistoryLeafNodeFact):
                entries.extend(node.ordered_entries)
                if len(entries) > max_entries:
                    raise PresentationHistoryTreeError(
                        "history tree entry-read bound exceeded"
                    )
                return
            for child in node.ordered_child_references:
                visit(child)

        if root is not None:
            visit(root)
        return tuple(entries)

    def find_entry(
        self,
        root: PresentationHistoryTreeNodeReferenceFact | None,
        *,
        placement_key: bytes,
        history_entry_id: str,
        max_node_reads: int,
        deadline_monotonic: float | None = None,
    ) -> tuple[PresentationHistoryEntryFact, int] | None:
        if max_node_reads <= 0:
            raise ValueError("history tree node-read bound must be positive")
        if root is None:
            return None
        reads = [0]
        reference = root
        rank_base = 0
        while True:
            node = self._load_bounded(
                reference,
                reads,
                max_node_reads,
                deadline_monotonic=deadline_monotonic,
            )
            if isinstance(node, PresentationHistoryLeafNodeFact):
                index = _lower_bound_entries(list(node.ordered_entries), placement_key)
                if index >= len(node.ordered_entries):
                    return None
                entry = node.ordered_entries[index]
                if (
                    entry.placement_key.canonical_comparable_key_bytes != placement_key
                    or entry.history_entry_id != history_entry_id
                ):
                    return None
                return entry, rank_base + index
            child_index = _child_index(
                list(node.ordered_child_references), placement_key
            )
            rank_base += sum(
                item.subtree_entry_count
                for item in node.ordered_child_references[:child_index]
            )
            reference = node.ordered_child_references[child_index]

    def read_page(
        self,
        root: PresentationHistoryTreeNodeReferenceFact | None,
        *,
        exclusive_placement_key: bytes | None,
        direction: str,
        max_entries: int,
        max_node_reads: int,
        deadline_monotonic: float | None = None,
    ) -> PresentationHistoryTreePage:
        """Read one key-bounded page without walking unrelated subtrees."""

        if direction not in {"before", "after"}:
            raise ValueError("history page direction must be before or after")
        if max_entries <= 0 or max_node_reads <= 0:
            raise ValueError("history page bounds must be positive")
        if root is None:
            return PresentationHistoryTreePage((), False, 0)
        reads = [0]
        selected: list[tuple[PresentationHistoryEntryFact, int]] = []
        target_count = max_entries + 1

        def visit_after(
            reference: PresentationHistoryTreeNodeReferenceFact, rank_base: int
        ) -> None:
            if len(selected) >= target_count:
                return
            if (
                exclusive_placement_key is not None
                and reference.last_placement_key.canonical_comparable_key_bytes
                <= exclusive_placement_key
            ):
                return
            node = self._load_bounded(
                reference,
                reads,
                max_node_reads,
                deadline_monotonic=deadline_monotonic,
            )
            if isinstance(node, PresentationHistoryLeafNodeFact):
                for index, entry in enumerate(node.ordered_entries):
                    key = entry.placement_key.canonical_comparable_key_bytes
                    if exclusive_placement_key is None or key > exclusive_placement_key:
                        selected.append((entry, rank_base + index))
                        if len(selected) >= target_count:
                            return
                return
            offset = rank_base
            for child in node.ordered_child_references:
                visit_after(child, offset)
                if len(selected) >= target_count:
                    return
                offset += child.subtree_entry_count

        def visit_before(
            reference: PresentationHistoryTreeNodeReferenceFact, rank_base: int
        ) -> None:
            if len(selected) >= target_count:
                return
            if (
                exclusive_placement_key is not None
                and reference.first_placement_key.canonical_comparable_key_bytes
                >= exclusive_placement_key
            ):
                return
            node = self._load_bounded(
                reference,
                reads,
                max_node_reads,
                deadline_monotonic=deadline_monotonic,
            )
            if isinstance(node, PresentationHistoryLeafNodeFact):
                for index in range(len(node.ordered_entries) - 1, -1, -1):
                    entry = node.ordered_entries[index]
                    key = entry.placement_key.canonical_comparable_key_bytes
                    if exclusive_placement_key is None or key < exclusive_placement_key:
                        selected.append((entry, rank_base + index))
                        if len(selected) >= target_count:
                            return
                return
            child_offsets: list[int] = []
            offset = rank_base
            for child in node.ordered_child_references:
                child_offsets.append(offset)
                offset += child.subtree_entry_count
            for child, child_offset in reversed(
                tuple(zip(node.ordered_child_references, child_offsets, strict=True))
            ):
                visit_before(child, child_offset)
                if len(selected) >= target_count:
                    return

        if direction == "after":
            visit_after(root, 0)
        else:
            visit_before(root, 0)
        has_more = len(selected) > max_entries
        selected = selected[:max_entries]
        if direction == "before":
            selected.reverse()
        return PresentationHistoryTreePage(
            ordered_ranked_entries=tuple(selected),
            has_more=has_more,
            node_read_count=reads[0],
        )

    def _load_bounded(
        self,
        reference: PresentationHistoryTreeNodeReferenceFact,
        reads: list[int],
        max_node_reads: int,
        *,
        deadline_monotonic: float | None,
    ) -> PresentationHistoryLeafNodeFact | PresentationHistoryInternalNodeFact:
        reads[0] += 1
        if reads[0] > max_node_reads:
            raise PresentationHistoryTreeError("history tree node-read bound exceeded")
        return self._load(
            reference,
            deadline_monotonic=deadline_monotonic,
        )

    def _insert(
        self,
        reference: PresentationHistoryTreeNodeReferenceFact | None,
        mutation: UpsertPresentationHistoryEntryMutationFact,
        *,
        deadline_monotonic: float | None,
    ) -> list[PresentationHistoryTreeNodeReferenceFact]:
        entry = mutation.resulting_entry
        if reference is None:
            if mutation.expected_previous_entry_fingerprint is not None:
                raise PresentationHistoryTreeError(
                    "history insertion unexpectedly requires a predecessor"
                )
            return [self._build_leaf((entry,))]
        node = self._load(
            reference,
            deadline_monotonic=deadline_monotonic,
        )
        key = entry.placement_key.canonical_comparable_key_bytes
        if isinstance(node, PresentationHistoryLeafNodeFact):
            values = list(node.ordered_entries)
            index = _lower_bound_entries(values, key)
            if index < len(values) and (
                values[index].placement_key.canonical_comparable_key_bytes == key
            ):
                if values[index].history_entry_id != entry.history_entry_id:
                    raise PresentationHistoryTreeError(
                        "duplicate placement key has a different history identity"
                    )
                if (
                    mutation.expected_previous_entry_fingerprint
                    != values[index].entry_fingerprint
                ):
                    raise PresentationHistoryTreeError(
                        "history replacement predecessor guard mismatch"
                    )
                values[index] = entry
            else:
                if mutation.expected_previous_entry_fingerprint is not None:
                    raise PresentationHistoryTreeError(
                        "history replacement target is absent"
                    )
                values.insert(index, entry)
            if len(values) <= self.contract.max_leaf_entries:
                return [self._build_leaf(tuple(values))]
            midpoint = len(values) // 2
            return [
                self._build_leaf(tuple(values[:midpoint])),
                self._build_leaf(tuple(values[midpoint:])),
            ]
        children = list(node.ordered_child_references)
        child_index = _child_index(children, key)
        replacement = self._insert(
            children[child_index],
            mutation,
            deadline_monotonic=deadline_monotonic,
        )
        children[child_index : child_index + 1] = replacement
        if len(children) <= self.contract.max_internal_fanout:
            return [
                self._build_internal(level=node.tree_level, children=tuple(children))
            ]
        midpoint = len(children) // 2
        return [
            self._build_internal(
                level=node.tree_level,
                children=tuple(children[:midpoint]),
            ),
            self._build_internal(
                level=node.tree_level,
                children=tuple(children[midpoint:]),
            ),
        ]

    def _delete(
        self,
        reference: PresentationHistoryTreeNodeReferenceFact | None,
        mutation: RemovePresentationHistoryEntryMutationFact,
        *,
        deadline_monotonic: float | None,
    ) -> list[PresentationHistoryTreeNodeReferenceFact]:
        if reference is None:
            raise PresentationHistoryTreeError(
                "cannot remove from an empty history tree"
            )
        node = self._load(
            reference,
            deadline_monotonic=deadline_monotonic,
        )
        key = mutation.placement_key.canonical_comparable_key_bytes
        if isinstance(node, PresentationHistoryLeafNodeFact):
            values = list(node.ordered_entries)
            index = _lower_bound_entries(values, key)
            if index >= len(values) or (
                values[index].placement_key.canonical_comparable_key_bytes != key
            ):
                raise PresentationHistoryTreeError("history removal target is absent")
            existing = values[index]
            if (
                existing.history_entry_id != mutation.history_entry_id
                or existing.entry_fingerprint
                != mutation.expected_previous_entry_fingerprint
            ):
                raise PresentationHistoryTreeError("history removal guard mismatch")
            values.pop(index)
            return [self._build_leaf(tuple(values))] if values else []
        children = list(node.ordered_child_references)
        child_index = _child_index(children, key)
        replacement = self._delete(
            children[child_index],
            mutation,
            deadline_monotonic=deadline_monotonic,
        )
        children[child_index : child_index + 1] = replacement
        if not children:
            return []
        return [self._build_internal(level=node.tree_level, children=tuple(children))]

    def _collapse_single_child_root(
        self,
        root: PresentationHistoryTreeNodeReferenceFact | None,
        height: int,
        *,
        deadline_monotonic: float | None,
    ) -> tuple[PresentationHistoryTreeNodeReferenceFact | None, int]:
        while root is not None and root.node_kind == "internal":
            node = self._load(
                root,
                deadline_monotonic=deadline_monotonic,
            )
            assert isinstance(node, PresentationHistoryInternalNodeFact)
            if len(node.ordered_child_references) != 1:
                break
            root = node.ordered_child_references[0]
            height -= 1
        return root, height

    def _build_leaf(
        self, entries: tuple[PresentationHistoryEntryFact, ...]
    ) -> PresentationHistoryTreeNodeReferenceFact:
        accumulator = _entries_accumulator(entries)
        node = build_frozen_fact(
            PresentationHistoryLeafNodeFact,
            schema_version="presentation_history_leaf_node.v1",
            node_kind="leaf",
            ordered_entries=entries,
            subtree_entry_accumulator=accumulator,
        )
        return self._prepare_node(node)

    def _build_internal(
        self,
        *,
        level: int,
        children: tuple[PresentationHistoryTreeNodeReferenceFact, ...],
    ) -> PresentationHistoryTreeNodeReferenceFact:
        accumulator = context_fingerprint(
            "presentation-history-internal-subtree:v1",
            tuple(
                (
                    item.subtree_entry_count,
                    item.subtree_entry_accumulator,
                    item.node_reference_fingerprint,
                )
                for item in children
            ),
        )
        node = build_frozen_fact(
            PresentationHistoryInternalNodeFact,
            schema_version="presentation_history_internal_node.v1",
            node_kind="internal",
            tree_level=level,
            ordered_child_references=children,
            subtree_entry_accumulator=accumulator,
        )
        return self._prepare_node(node)

    def _prepare_node(
        self,
        node: PresentationHistoryLeafNodeFact | PresentationHistoryInternalNodeFact,
    ) -> PresentationHistoryTreeNodeReferenceFact:
        encoded = canonical_json_bytes(node.model_dump(mode="json"))
        bound = (
            self.contract.max_leaf_node_bytes
            if isinstance(node, PresentationHistoryLeafNodeFact)
            else self.contract.max_internal_node_bytes
        )
        if len(encoded) > bound:
            raise PresentationHistoryTreeError(
                "presentation history node byte bound exceeded"
            )
        digest = f"sha256:{sha256(encoded).hexdigest()}"
        artifact_id = (
            "artifact:presentation-history-node:"
            f"{sha256(self.runtime_session_id.encode()).hexdigest()[:16]}:"
            f"{digest.removeprefix('sha256:')}"
        )
        if isinstance(node, PresentationHistoryLeafNodeFact):
            first = node.ordered_entries[0].placement_key
            last = node.ordered_entries[-1].placement_key
            count = len(node.ordered_entries)
        else:
            first = node.ordered_child_references[0].first_placement_key
            last = node.ordered_child_references[-1].last_placement_key
            count = sum(
                item.subtree_entry_count for item in node.ordered_child_references
            )
        reference = build_frozen_fact(
            PresentationHistoryTreeNodeReferenceFact,
            schema_version="presentation_history_tree_node_reference.v1",
            node_kind=node.node_kind,
            node_artifact_id=artifact_id,
            node_sha256=digest,
            node_byte_count=len(encoded),
            first_placement_key=first,
            last_placement_key=last,
            subtree_entry_count=count,
            subtree_entry_accumulator=node.subtree_entry_accumulator,
        )
        self._prepared.setdefault(
            artifact_id,
            PreparedPresentationHistoryArtifact(
                artifact_id=artifact_id,
                canonical_bytes=encoded,
                media_type=PRESENTATION_HISTORY_NODE_MEDIA_TYPE,
                semantic_metadata={
                    "artifact_kind": "presentation_history_node",
                    "node_kind": node.node_kind,
                    "node_fingerprint": node.node_fingerprint,
                    "tree_contract_fingerprint": self.contract.tree_contract_fingerprint,
                },
            ),
        )
        return reference

    def _load(
        self,
        reference: PresentationHistoryTreeNodeReferenceFact,
        *,
        deadline_monotonic: float | None,
    ) -> PresentationHistoryLeafNodeFact | PresentationHistoryInternalNodeFact:
        if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
            raise TimeoutError("presentation history tree read deadline exceeded")
        prepared = self._prepared.get(reference.node_artifact_id)
        if prepared is not None:
            encoded = prepared.canonical_bytes
        else:
            text = self.archive.get_text(
                reference.node_artifact_id,
                session_id=self.runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
            encoded = text.encode("utf-8")
        if (
            len(encoded) != reference.node_byte_count
            or f"sha256:{sha256(encoded).hexdigest()}" != reference.node_sha256
        ):
            raise PresentationHistoryTreeError(
                "history node artifact identity mismatch"
            )
        payload = json.loads(encoded)
        if payload.get("node_kind") == "leaf":
            node = PresentationHistoryLeafNodeFact.model_validate(payload)
        elif payload.get("node_kind") == "internal":
            node = PresentationHistoryInternalNodeFact.model_validate(payload)
        else:
            raise PresentationHistoryTreeError("history node has unknown kind")
        if node.node_kind != reference.node_kind:
            raise PresentationHistoryTreeError("history node kind/reference mismatch")
        if isinstance(node, PresentationHistoryLeafNodeFact):
            first = node.ordered_entries[0].placement_key
            last = node.ordered_entries[-1].placement_key
            count = len(node.ordered_entries)
        else:
            first = node.ordered_child_references[0].first_placement_key
            last = node.ordered_child_references[-1].last_placement_key
            count = sum(
                item.subtree_entry_count for item in node.ordered_child_references
            )
        if (
            first != reference.first_placement_key
            or last != reference.last_placement_key
            or count != reference.subtree_entry_count
            or node.subtree_entry_accumulator != reference.subtree_entry_accumulator
        ):
            raise PresentationHistoryTreeError(
                "history node/reference range or accumulator mismatch"
            )
        return node


def _lower_bound_entries(entries, key: bytes) -> int:
    low = 0
    high = len(entries)
    while low < high:
        mid = (low + high) // 2
        if entries[mid].placement_key.canonical_comparable_key_bytes < key:
            low = mid + 1
        else:
            high = mid
    return low


def _child_index(children, key: bytes) -> int:
    for index, child in enumerate(children):
        if key <= child.last_placement_key.canonical_comparable_key_bytes:
            return index
    return len(children) - 1


def _entries_accumulator(entries) -> str:
    return context_fingerprint(
        "presentation-history-ordered-entries:v1",
        tuple(
            (
                item.history_entry_id,
                item.entry_fingerprint,
                item.placement_key.placement_key_fingerprint,
            )
            for item in entries
        ),
    )


__all__ = [
    "PRESENTATION_HISTORY_NODE_MEDIA_TYPE",
    "PreparedPresentationHistoryArtifact",
    "PreparedPresentationHistoryTreeUpdate",
    "PresentationHistoryTreePage",
    "PresentationHistoryPersistentTree",
    "PresentationHistoryTreeError",
]
