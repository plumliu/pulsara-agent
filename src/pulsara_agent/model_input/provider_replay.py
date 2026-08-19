"""Immutable metadata-cut and selected hydration proofs for durable replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pulsara_agent.llm.provider_replay import (
    MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES,
    MAXIMUM_PROVIDER_REPLAY_JSON_DEPTH,
    MAXIMUM_PROVIDER_REPLAY_JSON_NODES,
    MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES,
    MAXIMUM_PROVIDER_REPLAY_STRING_UTF8_BYTES,
    ProviderAssistantReplayCodecKind,
    ProviderAssistantReplayFragment,
    ProviderReplayTargetCompatibilityFact,
    provider_replay_payload_digest,
)
from pulsara_agent.llm.request import (
    provider_assistant_message_public_projection_fingerprint,
)
from pulsara_agent.llm.input import MessageRole
from pulsara_agent.model_input.contracts import (
    FrozenCanonicalCompileSnapshot,
    FrozenCompiledModelInput,
    FrozenCompiledMessagePlacement,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.primitives.bounded_json import bounded_json_loads
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
)


class ProviderReplayHydrationFailureKind(StrEnum):
    CANONICAL_CORRUPTION = "CANONICAL_CORRUPTION"
    RESOURCE_BOUNDARY = "RESOURCE_BOUNDARY"


class ProviderReplayHydrationError(RuntimeError):
    """Typed, body-free failure for selected durable replay hydration."""

    def __init__(self, kind: ProviderReplayHydrationFailureKind) -> None:
        self.kind = kind
        super().__init__(f"provider replay hydration failed: {kind.value}")


def quote_provider_dispatch_composite_bytes(
    *,
    canonical_compile_bytes: int,
    manifest_metadata_bytes: int,
    selected_payload_bytes: int,
) -> int:
    values = (
        canonical_compile_bytes,
        manifest_metadata_bytes,
        selected_payload_bytes,
    )
    if any(value < 0 for value in values):
        raise ValueError("provider replay composite quote is negative")
    total = sum(values)
    if total > MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES:
        raise ProviderReplayHydrationError(
            ProviderReplayHydrationFailureKind.RESOURCE_BOUNDARY
        )
    return total


@dataclass(frozen=True, slots=True)
class FrozenDurableProviderReplayManifest:
    replay_id: str
    assistant_entry_id: str
    wire_api: str
    codec_kind: str
    provider_replay_contract_fingerprint: str
    replay_target_fingerprint: str
    public_projection_fingerprint: str
    payload_digest: str
    payload_size: int
    item_count: int
    fragment_fingerprint: str
    manifest_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.replay_id
            or not self.assistant_entry_id
            or self.payload_size < 2
            or self.payload_size > MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES
            or self.item_count < 1
        ):
            raise ValueError("provider replay manifest is invalid")
        for value in (
            self.provider_replay_contract_fingerprint,
            self.replay_target_fingerprint,
            self.public_projection_fingerprint,
            self.payload_digest,
            self.fragment_fingerprint,
            self.manifest_fingerprint,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("provider replay manifest fingerprint is invalid")
        if self.manifest_fingerprint != provider_replay_manifest_fingerprint(self):
            raise ValueError("provider replay manifest fingerprint mismatch")


def provider_replay_manifest_fingerprint(
    manifest: FrozenDurableProviderReplayManifest,
) -> str:
    return context_fingerprint(
        "pulsara.durable-provider-replay-manifest:v1",
        {
            "replay_id": manifest.replay_id,
            "assistant_entry_id": manifest.assistant_entry_id,
            "wire_api": manifest.wire_api,
            "codec_kind": manifest.codec_kind,
            "replay_contract": manifest.provider_replay_contract_fingerprint,
            "replay_target": manifest.replay_target_fingerprint,
            "public_projection": manifest.public_projection_fingerprint,
            "payload_digest": manifest.payload_digest,
            "payload_size": manifest.payload_size,
            "item_count": manifest.item_count,
            "fragment": manifest.fragment_fingerprint,
        },
    )


def freeze_provider_replay_manifest(**values: object) -> FrozenDurableProviderReplayManifest:
    provisional = FrozenDurableProviderReplayManifest.__new__(
        FrozenDurableProviderReplayManifest
    )
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "manifest_fingerprint", "")
    return FrozenDurableProviderReplayManifest(
        **values,  # type: ignore[arg-type]
        manifest_fingerprint=provider_replay_manifest_fingerprint(provisional),
    )


@dataclass(frozen=True, slots=True)
class FrozenDurableProviderReplayManifestCut:
    session_id: str
    scope: ProviderInputContinuityScope
    context_binding_revision_id: str
    provider_input_through_sequence: int
    manifests: tuple[FrozenDurableProviderReplayManifest, ...]
    aggregate_manifest_utf8_bytes: int
    cut_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.session_id != self.scope.session_id
            or not self.context_binding_revision_id
            or self.provider_input_through_sequence < 0
            or self.aggregate_manifest_utf8_bytes
            != manifest_cut_metadata_bytes(self.manifests)
        ):
            raise ValueError("provider replay manifest cut is invalid")
        ids = tuple(item.replay_id for item in self.manifests)
        entries = tuple(item.assistant_entry_id for item in self.manifests)
        if len(ids) != len(set(ids)) or len(entries) != len(set(entries)):
            raise ValueError("provider replay manifests are duplicated")
        if self.cut_fingerprint != provider_replay_manifest_cut_fingerprint(self):
            raise ValueError("provider replay manifest cut fingerprint mismatch")


def manifest_cut_metadata_bytes(
    manifests: tuple[FrozenDurableProviderReplayManifest, ...],
) -> int:
    return len(
        canonical_json_bytes(
            tuple(
                (
                    item.replay_id,
                    item.assistant_entry_id,
                    item.wire_api,
                    item.codec_kind,
                    item.provider_replay_contract_fingerprint,
                    item.replay_target_fingerprint,
                    item.public_projection_fingerprint,
                    item.payload_digest,
                    item.payload_size,
                    item.item_count,
                    item.fragment_fingerprint,
                    item.manifest_fingerprint,
                )
                for item in manifests
            )
        )
    )


def provider_replay_manifest_cut_fingerprint(
    cut: FrozenDurableProviderReplayManifestCut,
) -> str:
    return context_fingerprint(
        "pulsara.durable-provider-replay-manifest-cut:v1",
        {
            "session": cut.session_id,
            "scope": (
                cut.scope.scope_kind.value,
                cut.scope.scope_subagent_task_id,
            ),
            "binding_revision": cut.context_binding_revision_id,
            "through": cut.provider_input_through_sequence,
            "manifests": tuple(item.manifest_fingerprint for item in cut.manifests),
            "metadata_bytes": cut.aggregate_manifest_utf8_bytes,
        },
    )


def freeze_provider_replay_manifest_cut(
    *,
    session_id: str,
    scope: ProviderInputContinuityScope,
    context_binding_revision_id: str,
    provider_input_through_sequence: int,
    manifests: tuple[FrozenDurableProviderReplayManifest, ...],
) -> FrozenDurableProviderReplayManifestCut:
    metadata_bytes = manifest_cut_metadata_bytes(manifests)
    provisional = FrozenDurableProviderReplayManifestCut.__new__(
        FrozenDurableProviderReplayManifestCut
    )
    for name, value in {
        "session_id": session_id,
        "scope": scope,
        "context_binding_revision_id": context_binding_revision_id,
        "provider_input_through_sequence": provider_input_through_sequence,
        "manifests": manifests,
        "aggregate_manifest_utf8_bytes": metadata_bytes,
        "cut_fingerprint": "",
    }.items():
        object.__setattr__(provisional, name, value)
    return FrozenDurableProviderReplayManifestCut(
        session_id=session_id,
        scope=scope,
        context_binding_revision_id=context_binding_revision_id,
        provider_input_through_sequence=provider_input_through_sequence,
        manifests=manifests,
        aggregate_manifest_utf8_bytes=metadata_bytes,
        cut_fingerprint=provider_replay_manifest_cut_fingerprint(provisional),
    )


@dataclass(frozen=True, slots=True)
class FrozenCanonicalProviderDispatchRead:
    compile_snapshot: FrozenCanonicalCompileSnapshot
    replay_manifest_cut: FrozenDurableProviderReplayManifestCut
    composite_fingerprint: str

    def __post_init__(self) -> None:
        identity = self.compile_snapshot.canonical_input.identity
        cut = self.replay_manifest_cut
        if (
            identity.session_id != cut.session_id
            or identity.context_binding_revision_id
            != cut.context_binding_revision_id
            or identity.provider_input_through_sequence
            != cut.provider_input_through_sequence
            or identity.conversation_scope_kind is not cut.scope.scope_kind
            or identity.scope_subagent_task_id
            != cut.scope.scope_subagent_task_id
        ):
            raise ValueError("provider dispatch read cut does not exact-join")
        expected = context_fingerprint(
            "pulsara.canonical-provider-dispatch-read:v1",
            {
                "compile": self.compile_snapshot.canonical_read_cut_fingerprint,
                "replay_manifest_cut": cut.cut_fingerprint,
            },
        )
        if self.composite_fingerprint != expected:
            raise ValueError("provider dispatch read fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenSelectedDurableProviderReplayHydration:
    scope: ProviderInputContinuityScope
    source_manifest_cut_fingerprint: str
    replay_target_fingerprint: str
    selected_message_placements_fingerprint: str
    selected_manifests: tuple[FrozenDurableProviderReplayManifest, ...]
    fragments: tuple[ProviderAssistantReplayFragment, ...] = field(repr=False)
    aggregate_payload_bytes: int
    hydration_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.selected_manifests
            or len(self.selected_manifests) != len(self.fragments)
            or self.aggregate_payload_bytes
            != sum(item.payload_size for item in self.fragments)
        ):
            raise ValueError("selected provider replay hydration is empty or invalid")
        for manifest, fragment in zip(
            self.selected_manifests, self.fragments, strict=True
        ):
            if (
                manifest.assistant_entry_id != fragment.assistant_entry_id
                or manifest.fragment_fingerprint != fragment.fragment_fingerprint
                or manifest.replay_target_fingerprint
                != self.replay_target_fingerprint
            ):
                raise ValueError("selected provider replay fragment drifted")
        expected = context_fingerprint(
            "pulsara.selected-durable-provider-replay-hydration:v1",
            {
                "scope": (
                    self.scope.session_id,
                    self.scope.scope_kind.value,
                    self.scope.scope_subagent_task_id,
                ),
                "source_cut": self.source_manifest_cut_fingerprint,
                "replay_target": self.replay_target_fingerprint,
                "placements": self.selected_message_placements_fingerprint,
                "manifests": tuple(
                    item.manifest_fingerprint for item in self.selected_manifests
                ),
                "fragments": tuple(
                    item.fragment_fingerprint for item in self.fragments
                ),
                "payload_bytes": self.aggregate_payload_bytes,
            },
        )
        if self.hydration_fingerprint != expected:
            raise ValueError("selected provider replay hydration fingerprint mismatch")


def selected_message_placements_fingerprint(
    placements: tuple[FrozenCompiledMessagePlacement, ...],
) -> str:
    return context_fingerprint(
        "pulsara.selected-provider-replay-message-placements:v1",
        tuple(item.placement_fingerprint for item in placements),
    )


def select_compatible_provider_replay_manifests(
    *,
    manifest_cut: FrozenDurableProviderReplayManifestCut,
    compiled_input: FrozenCompiledModelInput,
    replay_target: ProviderReplayTargetCompatibilityFact,
) -> tuple[
    tuple[FrozenDurableProviderReplayManifest, ...],
    tuple[FrozenCompiledMessagePlacement, ...],
]:
    """Select exact, target-compatible replay manifests in provider order."""

    identity = compiled_input.canonical_input_identity
    if (
        manifest_cut.session_id != identity.session_id
        or manifest_cut.scope.scope_kind is not identity.conversation_scope_kind
        or manifest_cut.scope.scope_subagent_task_id
        != identity.scope_subagent_task_id
        or manifest_cut.context_binding_revision_id
        != identity.context_binding_revision_id
        or manifest_cut.provider_input_through_sequence
        != identity.provider_input_through_sequence
    ):
        raise ValueError("provider replay manifest cut does not join compiled input")

    placements_by_entry: dict[str, list[FrozenCompiledMessagePlacement]] = {}
    indexes_by_entry: dict[str, list[int]] = {}
    for index, placement in enumerate(compiled_input.message_placements):
        if placement.origin_entry_id is None:
            continue
        placements_by_entry.setdefault(placement.origin_entry_id, []).append(placement)
        indexes_by_entry.setdefault(placement.origin_entry_id, []).append(index)

    selected: list[FrozenDurableProviderReplayManifest] = []
    selected_placements: list[FrozenCompiledMessagePlacement] = []
    for manifest in manifest_cut.manifests:
        placements = placements_by_entry.get(manifest.assistant_entry_id)
        if placements is None:
            continue
        indexes = indexes_by_entry[manifest.assistant_entry_id]
        if (
            indexes != list(range(indexes[0], indexes[0] + len(indexes)))
            or tuple(item.within_origin_ordinal for item in placements)
            != tuple(range(len(placements)))
            or any(item.role is not MessageRole.ASSISTANT for item in placements)
        ):
            raise ValueError("provider replay assistant placement group is invalid")
        if (
            manifest.wire_api != replay_target.wire_api
            or manifest.codec_kind != replay_target.codec_kind.value
            or manifest.provider_replay_contract_fingerprint
            != replay_target.provider_replay_contract_fingerprint
            or manifest.replay_target_fingerprint
            != replay_target.replay_target_fingerprint
        ):
            continue
        selected.append(manifest)
        selected_placements.extend(placements)
    return tuple(selected), tuple(selected_placements)


def freeze_selected_provider_replay_hydration(
    *,
    manifest_cut: FrozenDurableProviderReplayManifestCut,
    compiled_input: FrozenCompiledModelInput,
    replay_target: ProviderReplayTargetCompatibilityFact,
    selected_manifests: tuple[FrozenDurableProviderReplayManifest, ...],
    selected_placements: tuple[FrozenCompiledMessagePlacement, ...],
    fragments: tuple[ProviderAssistantReplayFragment, ...],
) -> FrozenSelectedDurableProviderReplayHydration:
    """Freeze the only cut/scope/target/placement-bound hydration proof."""

    expected_manifests, expected_placements = (
        select_compatible_provider_replay_manifests(
            manifest_cut=manifest_cut,
            compiled_input=compiled_input,
            replay_target=replay_target,
        )
    )
    if (
        not selected_manifests
        or selected_manifests != expected_manifests
        or selected_placements != expected_placements
        or len(fragments) != len(selected_manifests)
    ):
        raise ValueError("selected provider replay hydration input drifted")

    message_by_entry: dict[str, list[int]] = {}
    for index, placement in enumerate(compiled_input.message_placements):
        if placement.origin_entry_id is not None:
            message_by_entry.setdefault(placement.origin_entry_id, []).append(index)
    for manifest, fragment in zip(selected_manifests, fragments, strict=True):
        indexes = message_by_entry[manifest.assistant_entry_id]
        if len(indexes) != 1:
            raise ValueError("provider replay requires one assistant message group")
        message = compiled_input.messages[indexes[0]]
        if (
            fragment.replay_target_fingerprint
            != replay_target.replay_target_fingerprint
            or fragment.codec_kind is not replay_target.codec_kind
            or fragment.provider_replay_contract_fingerprint
            != replay_target.provider_replay_contract_fingerprint
            or provider_assistant_message_public_projection_fingerprint(message)
            != manifest.public_projection_fingerprint
            or fragment.public_projection_fingerprint
            != manifest.public_projection_fingerprint
        ):
            raise ValueError("provider replay public projection or target drifted")

    placement_fingerprint = selected_message_placements_fingerprint(
        selected_placements
    )
    aggregate_payload_bytes = sum(item.payload_size for item in fragments)
    provisional = FrozenSelectedDurableProviderReplayHydration.__new__(
        FrozenSelectedDurableProviderReplayHydration
    )
    values = {
        "scope": manifest_cut.scope,
        "source_manifest_cut_fingerprint": manifest_cut.cut_fingerprint,
        "replay_target_fingerprint": replay_target.replay_target_fingerprint,
        "selected_message_placements_fingerprint": placement_fingerprint,
        "selected_manifests": selected_manifests,
        "fragments": fragments,
        "aggregate_payload_bytes": aggregate_payload_bytes,
        "hydration_fingerprint": "",
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    values["hydration_fingerprint"] = context_fingerprint(
        "pulsara.selected-durable-provider-replay-hydration:v1",
        {
            "scope": (
                manifest_cut.scope.session_id,
                manifest_cut.scope.scope_kind.value,
                manifest_cut.scope.scope_subagent_task_id,
            ),
            "source_cut": manifest_cut.cut_fingerprint,
            "replay_target": replay_target.replay_target_fingerprint,
            "placements": placement_fingerprint,
            "manifests": tuple(
                item.manifest_fingerprint for item in selected_manifests
            ),
            "fragments": tuple(item.fragment_fingerprint for item in fragments),
            "payload_bytes": aggregate_payload_bytes,
        },
    )
    return FrozenSelectedDurableProviderReplayHydration(**values)  # type: ignore[arg-type]


def decode_provider_replay_fragment(
    *,
    manifest: FrozenDurableProviderReplayManifest,
    payload_bytes: bytes,
) -> ProviderAssistantReplayFragment:
    if (
        len(payload_bytes) != manifest.payload_size
        or provider_replay_payload_digest(payload_bytes) != manifest.payload_digest
    ):
        raise ValueError("provider replay payload integrity mismatch")
    value = bounded_json_loads(
        payload_bytes,
        maximum_bytes=MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES,
        maximum_nodes=MAXIMUM_PROVIDER_REPLAY_JSON_NODES,
        maximum_depth=MAXIMUM_PROVIDER_REPLAY_JSON_DEPTH,
        maximum_string_utf8_bytes=MAXIMUM_PROVIDER_REPLAY_STRING_UTF8_BYTES,
    )
    if not isinstance(value, list) or len(value) != manifest.item_count:
        raise ValueError("provider replay payload top-level shape is invalid")
    frozen_items: list[FrozenJsonObjectFact] = []
    for item in value:
        frozen = freeze_json(item)
        if not isinstance(frozen, FrozenJsonObjectFact):
            raise ValueError("provider replay item is not an object")
        frozen_items.append(frozen)
    if canonical_json_bytes(value) != payload_bytes:
        raise ValueError("provider replay payload is not canonical JSON")
    return ProviderAssistantReplayFragment(
        codec_kind=ProviderAssistantReplayCodecKind(manifest.codec_kind),
        provider_replay_contract_fingerprint=(
            manifest.provider_replay_contract_fingerprint
        ),
        replay_target_fingerprint=manifest.replay_target_fingerprint,
        assistant_entry_id=manifest.assistant_entry_id,
        public_projection_fingerprint=manifest.public_projection_fingerprint,
        ordered_items=tuple(frozen_items),
        payload_bytes=payload_bytes,
        payload_digest=manifest.payload_digest,
        payload_size=manifest.payload_size,
        item_count=manifest.item_count,
        fragment_fingerprint=manifest.fragment_fingerprint,
    )


__all__ = [
    "FrozenCanonicalProviderDispatchRead",
    "FrozenDurableProviderReplayManifest",
    "FrozenDurableProviderReplayManifestCut",
    "FrozenSelectedDurableProviderReplayHydration",
    "ProviderReplayHydrationError",
    "ProviderReplayHydrationFailureKind",
    "quote_provider_dispatch_composite_bytes",
    "decode_provider_replay_fragment",
    "freeze_provider_replay_manifest",
    "freeze_provider_replay_manifest_cut",
    "freeze_selected_provider_replay_hydration",
    "select_compatible_provider_replay_manifests",
    "selected_message_placements_fingerprint",
]
