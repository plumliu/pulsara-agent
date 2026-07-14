"""Typed observation-rollup rendering and artifact materialization.

Rollups are derived observations.  They never replace a provider-native tool
call/result pair and never infer execution semantics from display payloads.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Literal, Protocol

from pulsara_agent.primitives.context import (
    TranscriptCompileInput,
    TranscriptToolCallFact,
    context_fingerprint,
)
from pulsara_agent.primitives.long_horizon import (
    LongHorizonContextAllocationPolicyFact,
    ObservationRollupFact,
    ObservationRollupMemberFact,
    ObservationRollupPlacementAnchorFact,
    ObservationRollupRendererContractFact,
    PreparedObservationRollupUnit,
    RuntimeDerivedObservationCompileUnit,
    default_observation_rollup_renderer_contract,
    observation_rollup_id,
)
from pulsara_agent.primitives.model_call import (
    RuntimeDerivedObservationCarrierContractFact,
)
from pulsara_agent.primitives.tool_result import ToolResultRenderUnit

if TYPE_CHECKING:
    from pulsara_agent.llm.estimator import TokenEstimator
    from pulsara_agent.runtime.session import RuntimeSession


class ObservationRollupContractError(RuntimeError):
    """A durable rollup renderer or source contract cannot be rebound."""


class InMemoryObservationRollupContentCache:
    """Bounded verified artifact bytes; durable rollup facts remain authority."""

    def __init__(self, *, max_entries: int = 128, max_chars: int = 2_000_000) -> None:
        if max_entries < 1 or max_chars < 1:
            raise ValueError("rollup content cache bounds must be positive")
        self._max_entries = max_entries
        self._max_chars = max_chars
        self._entries: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._chars = 0
        self._lock = RLock()

    def get(self, artifact_id: str, content_sha256: str) -> str | None:
        key = (artifact_id, content_sha256)
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, artifact_id: str, content_sha256: str, text: str) -> None:
        if len(text) > self._max_chars:
            return
        key = (artifact_id, content_sha256)
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._chars -= len(previous)
            self._entries[key] = text
            self._chars += len(text)
            while len(self._entries) > self._max_entries or self._chars > self._max_chars:
                _, evicted = self._entries.popitem(last=False)
                self._chars -= len(evicted)


class InMemoryPreparedObservationRollupCache:
    """Bounded memoization of fully verified provider-ready rollup units."""

    def __init__(self, *, max_entries: int = 128, max_chars: int = 2_000_000) -> None:
        if max_entries < 1 or max_chars < 1:
            raise ValueError("prepared rollup cache bounds must be positive")
        self._max_entries = max_entries
        self._max_chars = max_chars
        self._entries: OrderedDict[str, PreparedObservationRollupUnit] = (
            OrderedDict()
        )
        self._chars = 0
        self._lock = RLock()

    def get(self, key: str) -> PreparedObservationRollupUnit | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key: str, value: PreparedObservationRollupUnit) -> None:
        chars = value.compile_unit.inline_chars
        if chars > self._max_chars:
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._chars -= previous.compile_unit.inline_chars
            self._entries[key] = value
            self._chars += chars
            while len(self._entries) > self._max_entries or self._chars > self._max_chars:
                _, evicted = self._entries.popitem(last=False)
                self._chars -= evicted.compile_unit.inline_chars


def prepared_observation_rollup_cache_key(
    *,
    durable_rollup_fingerprint: str,
    member_unit_fingerprints: tuple[str, ...],
    placement_basis_fingerprint: str,
    policy_fingerprint: str,
    estimator_fingerprint: str,
    carrier_contract_fingerprint: str,
) -> str:
    return context_fingerprint(
        "prepared-observation-rollup-cache-key:v1",
        {
            "durable_rollup_fingerprint": durable_rollup_fingerprint,
            "member_unit_fingerprints": member_unit_fingerprints,
            "placement_basis_fingerprint": placement_basis_fingerprint,
            "policy_fingerprint": policy_fingerprint,
            "estimator_fingerprint": estimator_fingerprint,
            "carrier_contract_fingerprint": carrier_contract_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class ObservationRollupRenderResult:
    text: str
    content_sha256: str
    chars: int
    estimated_tokens: int
    evidence_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.chars != len(self.text):
            raise ValueError("rollup render char count mismatch")
        digest = f"sha256:{sha256(self.text.encode('utf-8')).hexdigest()}"
        if self.content_sha256 != digest:
            raise ValueError("rollup render content hash mismatch")


class ObservationRollupRenderer(Protocol):
    def render(
        self,
        *,
        rollup_kind: str,
        members: tuple[ObservationRollupMemberFact, ...],
        source_units: tuple[ToolResultRenderUnit, ...],
        policy: LongHorizonContextAllocationPolicyFact,
        token_estimator: TokenEstimator,
    ) -> ObservationRollupRenderResult: ...


@dataclass(frozen=True, slots=True)
class ObservationRollupRendererBinding:
    contract: ObservationRollupRendererContractFact
    implementation_build_fingerprint: str
    renderer: ObservationRollupRenderer


class ObservationRollupRendererRegistry:
    """Exact semantic-contract registry; process build identity is diagnostic."""

    def __init__(self) -> None:
        self._bindings: dict[
            tuple[str, str], ObservationRollupRendererBinding
        ] = {}

    def register(self, binding: ObservationRollupRendererBinding) -> None:
        key = (binding.contract.renderer_id, binding.contract.renderer_version)
        previous = self._bindings.get(key)
        if previous is not None and previous.contract != binding.contract:
            raise ObservationRollupContractError(
                "rollup renderer ID/version has conflicting semantic contracts"
            )
        self._bindings[key] = binding

    def resolve_binding(
        self,
        *,
        renderer_id: str,
        renderer_version: str,
        renderer_contract_fingerprint: str,
    ) -> ObservationRollupRendererBinding:
        binding = self._bindings.get((renderer_id, renderer_version))
        if binding is None:
            raise ObservationRollupContractError(
                "observation rollup renderer binding is unavailable"
            )
        if (
            binding.contract.renderer_contract_fingerprint
            != renderer_contract_fingerprint
        ):
            raise ObservationRollupContractError(
                "observation rollup renderer contract fingerprint mismatch"
            )
        return binding


class _CanonicalObservationRollupRenderer:
    def render(
        self,
        *,
        rollup_kind: str,
        members: tuple[ObservationRollupMemberFact, ...],
        source_units: tuple[ToolResultRenderUnit, ...],
        policy: LongHorizonContextAllocationPolicyFact,
        token_estimator: TokenEstimator,
    ) -> ObservationRollupRenderResult:
        if len(members) < 2 or len(members) > policy.max_rollup_members:
            raise ValueError("rollup member count is outside the frozen policy")
        if len(members) != len(source_units):
            raise ValueError("rollup member/source unit cardinality mismatch")
        rows: list[dict[str, object]] = []
        evidence: set[str] = set()
        for member, unit in zip(members, source_units, strict=True):
            semantics = unit.rollup_semantics
            if semantics is None:
                raise ValueError("rollup source unit lacks typed rollup semantics")
            if (
                member.unit_id != unit.unit_id
                or member.tool_call_id != unit.tool_call_id
                or member.result_event_id != unit.source_event_ids[-1]
                or member.result_sequence != unit.source_sequence_end
                or member.result_state != unit.result_state.value
                or semantics.rollup_kind != rollup_kind
            ):
                raise ValueError("rollup member/source unit identity mismatch")
            evidence.update(semantics.evidence_keys)
            rows.append(
                {
                    "unit_id": member.unit_id,
                    "tool_call_id": member.tool_call_id,
                    "tool_name": unit.model_tool_name,
                    "result_state": member.result_state,
                    "result_sequence": member.result_sequence,
                    "observed_at_utc": unit.observation_timing.observed_at_utc,
                    "essential_semantic_fingerprint": (
                        member.essential_semantic_fingerprint
                    ),
                    "primary_artifact_id": member.primary_artifact_id,
                    "evidence_keys": semantics.evidence_keys,
                }
            )
        evidence_keys = tuple(sorted(evidence))
        if len(evidence_keys) > 64:
            raise ValueError("rollup evidence key union exceeds durable bound")
        payload = {
            "pulsara_runtime_observation": {
                "schema_version": "observation_rollup_payload.v1",
                "kind": "tool_result_rollup",
                "rollup_kind": rollup_kind,
                "member_count": len(rows),
                "members": rows,
                "evidence_keys": evidence_keys,
                "authority": (
                    "derived index only; original tool call/result pairs remain "
                    "canonical"
                ),
            }
        }
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ObservationRollupRenderResult(
            text=text,
            content_sha256=f"sha256:{sha256(text.encode('utf-8')).hexdigest()}",
            chars=len(text),
            estimated_tokens=token_estimator.estimate_text(text),
            evidence_keys=evidence_keys,
        )


def default_observation_rollup_renderer_registry() -> (
    ObservationRollupRendererRegistry
):
    registry = ObservationRollupRendererRegistry()
    contract = default_observation_rollup_renderer_contract()
    registry.register(
        ObservationRollupRendererBinding(
            contract=contract,
            implementation_build_fingerprint=(
                "pulsara.observation_rollup.canonical:python:v1"
            ),
            renderer=_CanonicalObservationRollupRenderer(),
        )
    )
    return registry


def build_rollup_member_fact(unit: ToolResultRenderUnit) -> ObservationRollupMemberFact:
    semantics = unit.rollup_semantics
    if semantics is None:
        raise ValueError("rollup member requires typed rollup semantics")
    primary = _primary_text_artifact_id(unit)
    return ObservationRollupMemberFact(
        unit_id=unit.unit_id,
        tool_call_id=unit.tool_call_id,
        result_event_id=unit.source_event_ids[-1],
        result_sequence=unit.source_sequence_end,
        result_state=unit.result_state.value,
        essential_semantic_fingerprint=context_fingerprint(
            "tool-result-essential-semantic:v1",
            {
                "result_state": unit.result_state,
                "essential": unit.essential,
                "observation_timing": unit.observation_timing,
                "terminal_payload_timing": unit.terminal_payload_timing,
                "primary_artifact_id": primary,
                "rollup_semantics": semantics,
            },
        ),
        primary_artifact_id=primary,
    )


def derive_rollup_placement_anchor(
    *,
    transcript: TranscriptCompileInput,
    member_units: tuple[ToolResultRenderUnit, ...],
) -> ObservationRollupPlacementAnchorFact:
    """Anchor after the complete pair group containing the final member."""

    if len(member_units) < 2:
        raise ValueError("rollup placement requires at least two members")
    messages = {message.message_id: message for message in transcript.messages}
    message_indexes = {
        message.message_id: index for index, message in enumerate(transcript.messages)
    }
    pairs = {pair.tool_call_id: pair for pair in transcript.tool_pairs}
    last = max(member_units, key=lambda unit: (unit.source_sequence_end, unit.unit_id))
    call_message = messages.get(last.call_message_id)
    if call_message is None or call_message.role != "assistant":
        raise ValueError("rollup anchor call message is not an assistant message")
    group_call_ids = tuple(
        block.tool_call_id
        for block in call_message.blocks
        if isinstance(block, TranscriptToolCallFact)
    )
    if not group_call_ids:
        raise ValueError("rollup anchor pair group contains no tool calls")
    group_pairs = []
    for call_id in group_call_ids:
        pair = pairs.get(call_id)
        if pair is None or pair.call_message_id != call_message.message_id:
            raise ValueError("rollup anchor pair group is incomplete")
        group_pairs.append(pair)
    anchor_pair = max(
        group_pairs,
        key=lambda pair: (
            message_indexes[pair.result_message_id],
            pair.result_block_index,
            pair.tool_call_id,
        ),
    )
    anchor_message = messages[anchor_pair.result_message_id]
    pair_group_id = context_fingerprint(
        "tool-interaction-pair-group:v1",
        {
            "call_message_id": call_message.message_id,
            "tool_call_ids": group_call_ids,
            "pair_fingerprints": tuple(pair.pair_fingerprint for pair in group_pairs),
        },
    )
    payload = {
        "placement": "after_complete_pair_group",
        "pair_group_id": pair_group_id,
        "insert_after_transcript_message_id": anchor_message.message_id,
        "insert_after_source_sequence": anchor_message.source_sequence_end,
    }
    return ObservationRollupPlacementAnchorFact(
        **payload,
        anchor_fingerprint=context_fingerprint(
            "observation-rollup-placement-anchor:v1", payload
        ),
    )


@dataclass(frozen=True, slots=True)
class PreparedObservationRollupArtifact:
    fact: ObservationRollupFact
    anchor: ObservationRollupPlacementAnchorFact
    rendered: ObservationRollupRenderResult


def prepare_observation_rollup_artifact(
    *,
    window_id: str,
    member_units: tuple[ToolResultRenderUnit, ...],
    transcript: TranscriptCompileInput,
    policy: LongHorizonContextAllocationPolicyFact,
    token_estimator: TokenEstimator,
    registry: ObservationRollupRendererRegistry,
    placement_anchor: ObservationRollupPlacementAnchorFact | None = None,
) -> PreparedObservationRollupArtifact:
    ordered = tuple(
        sorted(member_units, key=lambda unit: (unit.source_sequence_end, unit.unit_id))
    )
    if len(ordered) < 2 or len(ordered) > policy.max_rollup_members:
        raise ValueError("rollup member count is outside the frozen policy")
    semantics = tuple(unit.rollup_semantics for unit in ordered)
    if any(item is None for item in semantics):
        raise ValueError("rollup source lacks typed family semantics")
    typed_semantics = tuple(item for item in semantics if item is not None)
    identity = {
        (
            item.rollup_kind,
            item.family_key,
            item.renderer_id,
            item.renderer_version,
            item.renderer_contract_fingerprint,
        )
        for item in typed_semantics
    }
    if len(identity) != 1:
        raise ValueError("rollup members do not share one semantic family")
    (
        rollup_kind,
        _family_key,
        renderer_id,
        renderer_version,
        renderer_contract_fingerprint,
    ) = next(iter(identity))
    binding = registry.resolve_binding(
        renderer_id=renderer_id,
        renderer_version=renderer_version,
        renderer_contract_fingerprint=renderer_contract_fingerprint,
    )
    members = tuple(build_rollup_member_fact(unit) for unit in ordered)
    ordered_member_set_fingerprint = context_fingerprint(
        "observation-rollup-members:v1",
        tuple(
            (
                member.unit_id,
                member.tool_call_id,
                member.result_event_id,
                member.essential_semantic_fingerprint,
            )
            for member in members
        ),
    )
    rollup_id = observation_rollup_id(
        window_id=window_id,
        rollup_kind=rollup_kind,
        ordered_member_set_fingerprint=ordered_member_set_fingerprint,
        renderer_contract_fingerprint=renderer_contract_fingerprint,
    )
    rendered = binding.renderer.render(
        rollup_kind=rollup_kind,
        members=members,
        source_units=ordered,
        policy=policy,
        token_estimator=token_estimator,
    )
    artifact_id = (
        "artifact:observation-rollup:"
        + context_fingerprint(
            "observation-rollup-artifact-id:v1",
            {
                "rollup_id": rollup_id,
                "content_sha256": rendered.content_sha256,
                "renderer_contract_fingerprint": renderer_contract_fingerprint,
            },
        ).removeprefix("sha256:")
    )
    payload = {
        "schema_version": "observation_rollup.v1",
        "rollup_id": rollup_id,
        "window_id": window_id,
        "rollup_kind": rollup_kind,
        "member_facts": members,
        "ordered_member_set_fingerprint": ordered_member_set_fingerprint,
        "renderer_id": renderer_id,
        "renderer_version": renderer_version,
        "renderer_contract_fingerprint": renderer_contract_fingerprint,
        "rendered_artifact_id": artifact_id,
        "rendered_content_sha256": rendered.content_sha256,
        "estimated_tokens": rendered.estimated_tokens,
        "evidence_keys": rendered.evidence_keys,
    }
    fact = ObservationRollupFact(
        **payload,
        semantic_fingerprint=context_fingerprint("observation-rollup:v1", payload),
    )
    return PreparedObservationRollupArtifact(
        fact=fact,
        anchor=(
            placement_anchor
            or derive_rollup_placement_anchor(
                transcript=transcript,
                member_units=ordered,
            )
        ),
        rendered=rendered,
    )


async def materialize_observation_rollup(
    *,
    runtime_session: RuntimeSession,
    run_id: str,
    prepared: PreparedObservationRollupArtifact,
    carrier: RuntimeDerivedObservationCarrierContractFact,
    artifact_mode: Literal["write_confirm", "read_confirm"] = "write_confirm",
    deadline_monotonic: float | None = None,
) -> PreparedObservationRollupUnit:
    deadline = deadline_monotonic or monotonic() + 30.0
    fact = prepared.fact
    rendered = prepared.rendered

    cached = runtime_session.observation_rollup_content_cache.get(
        fact.rendered_artifact_id,
        rendered.content_sha256,
    )

    def materialize() -> str:
        if artifact_mode == "write_confirm":
            confirmation = (
                runtime_session.archive.put_text_if_absent_or_confirm_identical(
                    fact.rendered_artifact_id,
                    rendered.text,
                    session_id=runtime_session.runtime_session_id,
                    run_id=run_id,
                    media_type="application/vnd.pulsara.observation-rollup+json",
                    semantic_metadata={
                        "artifact_kind": "observation_rollup",
                        "rollup_id": fact.rollup_id,
                        "window_id": fact.window_id,
                        "rollup_semantic_fingerprint": fact.semantic_fingerprint,
                        "renderer_contract_fingerprint": (
                            fact.renderer_contract_fingerprint
                        ),
                        "ordered_member_set_fingerprint": (
                            fact.ordered_member_set_fingerprint
                        ),
                        "content_sha256": rendered.content_sha256,
                    },
                    deadline_monotonic=deadline,
                )
            )
            if confirmation.result.digest != rendered.content_sha256:
                raise ObservationRollupContractError(
                    "confirmed rollup artifact content hash mismatch"
                )
        stored = runtime_session.archive.get_text(
            fact.rendered_artifact_id,
            session_id=runtime_session.runtime_session_id,
            deadline_monotonic=deadline,
        )
        if stored != rendered.text:
            raise ObservationRollupContractError(
                "confirmed rollup artifact bytes differ from prepared content"
            )
        return stored

    if cached is not None:
        if cached != rendered.text:
            raise ObservationRollupContractError(
                "cached rollup artifact bytes differ from prepared content"
            )
        text = cached
    else:
        text = await runtime_session.context_input_io_service.execute(
            operation_name="materialize-observation-rollup",
            operation=materialize,
            deadline_monotonic=deadline,
        )
        runtime_session.observation_rollup_content_cache.put(
            fact.rendered_artifact_id,
            rendered.content_sha256,
            text,
        )
    compile_payload = {
        "unit_id": f"runtime_observation:{fact.rollup_id}",
        "source_kind": "long_horizon_observation_rollup",
        "source_semantic_fingerprint": fact.semantic_fingerprint,
        "inline_text": text,
        "inline_content_sha256": rendered.content_sha256,
        "inline_chars": len(text),
        "placement_anchor": prepared.anchor,
        "lowering_kind": "runtime_owned_derived_observation",
        "carrier_contract_fingerprint": carrier.contract_fingerprint,
    }
    compile_unit = RuntimeDerivedObservationCompileUnit(
        **compile_payload,
        unit_fingerprint=context_fingerprint(
            "runtime-derived-observation-compile-unit:v1", compile_payload
        ),
    )
    prepared_payload = {
        "schema_version": "prepared_observation_rollup.v1",
        "rollup": fact,
        "artifact_id": fact.rendered_artifact_id,
        "artifact_content_sha256": rendered.content_sha256,
        "ordered_member_unit_ids": tuple(
            member.unit_id for member in fact.member_facts
        ),
        "ordered_member_set_fingerprint": fact.ordered_member_set_fingerprint,
        "compile_unit": compile_unit,
    }
    return PreparedObservationRollupUnit(
        **prepared_payload,
        prepared_fingerprint=context_fingerprint(
            "prepared-observation-rollup:v1", prepared_payload
        ),
    )


def _primary_text_artifact_id(unit: ToolResultRenderUnit) -> str | None:
    candidates = tuple(
        item
        for item in unit.artifacts
        if item.role not in {"diagnostics", "metadata"}
        and (
            item.media_type.startswith("text/")
            or "json" in item.media_type
            or "xml" in item.media_type
            or "yaml" in item.media_type
        )
    )
    selected = next((item for item in candidates if item.preview is not None), None)
    selected = selected or (candidates[0] if candidates else None)
    return selected.artifact_id if selected is not None else None


__all__ = [
    "InMemoryObservationRollupContentCache",
    "InMemoryPreparedObservationRollupCache",
    "ObservationRollupContractError",
    "ObservationRollupRenderResult",
    "ObservationRollupRenderer",
    "ObservationRollupRendererBinding",
    "ObservationRollupRendererRegistry",
    "PreparedObservationRollupArtifact",
    "build_rollup_member_fact",
    "default_observation_rollup_renderer_registry",
    "derive_rollup_placement_anchor",
    "materialize_observation_rollup",
    "prepared_observation_rollup_cache_key",
    "prepare_observation_rollup_artifact",
]
