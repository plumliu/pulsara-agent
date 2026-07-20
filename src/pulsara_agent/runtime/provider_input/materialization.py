"""Provider-neutral immutable input fragment lowering and hydration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives._context_base import freeze_json, thaw_json
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint
from pulsara_agent.primitives._context_base import ContextEventReferenceFact
from pulsara_agent.primitives.context_source import (
    ContextArtifactReferenceFact,
    LedgerAuthorityHorizonFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    CanonicalProviderInputPlanFact,
    CompactionReplacementSummarySourceAttributionFact,
    DerivedToolResultMessageSourceAttributionFact,
    DirectStableMessageSourceAttributionFact,
    LifecycleNoteSourceAttributionFact,
    ProviderOrderedTranscriptUnitFact,
    ProviderInputTextBlockFact,
    ProviderInputThinkingBlockFact,
    ProviderInputToolCallBlockFact,
    ProviderInputReplayBindingIdentityFact,
    ProviderInputUnitAttributionFact,
    ProviderInputUnitMaterializationFact,
    ProviderInputUnitSemanticDocumentIdentityFact,
    ProviderInputUnitSemanticMaterializationFact,
    ProviderInputUnitSemanticFact,
    ProviderMessageFragmentFact,
    ProviderToolSpecFragmentFact,
)


LOWERING_CONTRACT_ID = "pulsara.provider-neutral-input"
LOWERING_CONTRACT_VERSION = "2"
LOWERING_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-lowering-contract:v2",
    {
        "messages": "typed-role-content-thinking-tool-calls:v1",
        "tools": "ordered-name-description-frozen-schema:v1",
        "system": "ordered-per-context-source-fragments:v1",
        "append_revision": "provider-visible-latest-wins-envelope:v1",
    },
)
PROVIDER_UNIT_SEMANTIC_DOCUMENT_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-unit-semantic-document-contract:v1",
    "typed-unit-semantic+typed-provider-fragment+canonical-wire-digest",
)
PROVIDER_UNIT_WIRE_CODEC_CONTRACT_FINGERPRINT = context_fingerprint(
    "provider-input-unit-wire-codec-contract:v1",
    "utf-8-final-provider-fragment-v1",
)


@dataclass(frozen=True, slots=True)
class RecursivelyImmutableProviderInputCarrier:
    system_prompt: str | None
    ordered_messages: tuple[LLMMessage, ...]
    ordered_tool_fragments: tuple[ProviderToolSpecFragmentFact, ...]
    input_unit_count: int
    ordered_provider_content_accumulator: str
    provider_input_semantic_fingerprint: str
    carrier_fingerprint: str

    def to_llm_context(self, template: LLMContext) -> LLMContext:
        # ToolSpec is a frozen dataclass, but its JSON-schema dictionary is not.
        # Materialize a fresh object graph for every dispatch so callers can
        # never mutate the resident carrier through an LLMContext alias.
        return LLMContext(
            messages=self.ordered_messages,
            tools=tuple(
                _hydrate_tool_fragment(fragment)
                for fragment in self.ordered_tool_fragments
            ),
            system_prompt=self.system_prompt,
            context_id=template.context_id,
            resolved_model_call_id=template.resolved_model_call_id,
            target_fingerprint=template.target_fingerprint,
            model_call_index=template.model_call_index,
            compiler_estimated_input_tokens=template.compiler_estimated_input_tokens,
        )


def freeze_message_unit(
    message: LLMMessage,
    *,
    unit_kind: str,
    owner_semantic_fingerprint: str,
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...],
    estimated_tokens: int,
    source_event_refs: tuple[ContextEventReferenceFact, ...] = (),
    source_artifact_refs: tuple[ContextArtifactReferenceFact, ...] = (),
    pairing_group_id: str | None = None,
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...] = (),
) -> ProviderInputUnitMaterializationFact:
    fragment = freeze_provider_message_fragment(message)
    semantic = build_frozen_fact(
        ProviderInputUnitSemanticFact,
        schema_version="provider_input_unit_semantic.v3",
        unit_kind=unit_kind,
        provider_content_semantic_fingerprint=fragment.semantic_fingerprint,
        lowering_contract_id=LOWERING_CONTRACT_ID,
        lowering_contract_version=LOWERING_CONTRACT_VERSION,
        lowering_contract_fingerprint=LOWERING_CONTRACT_FINGERPRINT,
        pairing_group_id=pairing_group_id,
    )
    attribution = build_frozen_fact(
        ProviderInputUnitAttributionFact,
        schema_version="provider_input_unit_attribution.v1",
        semantic=semantic,
        owner_semantic_fingerprint=owner_semantic_fingerprint,
        source_event_refs=source_event_refs,
        source_artifact_refs=source_artifact_refs,
        authority_horizons=authority_horizons,
        required_replay_bindings=required_replay_bindings,
    )
    return build_frozen_fact(
        ProviderInputUnitMaterializationFact,
        schema_version="provider_input_unit_materialization.v1",
        attribution=attribution,
        canonical_provider_fragment=fragment,
        estimated_tokens=max(0, estimated_tokens),
    )


def build_provider_unit_semantic_document(
    unit: ProviderInputUnitMaterializationFact,
) -> tuple[
    ProviderInputUnitSemanticMaterializationFact,
    ProviderInputUnitSemanticDocumentIdentityFact,
]:
    """Build the placement-free semantic document for an exact provider unit."""

    fragment = unit.canonical_provider_fragment
    fragment_bytes = canonical_json_bytes(fragment.model_dump(mode="json"))
    if isinstance(fragment, ProviderMessageFragmentFact) and (
        fragment.role in {"runtime_observation", "runtime_request", "user"}
        and len(fragment.content_blocks) == 1
        and isinstance(fragment.content_blocks[0], ProviderInputTextBlockFact)
    ):
        wire_bytes = fragment.content_blocks[0].text.encode("utf-8")
    else:
        wire_bytes = fragment_bytes
    semantic_materialization = build_frozen_fact(
        ProviderInputUnitSemanticMaterializationFact,
        schema_version="provider_input_unit_semantic_materialization.v1",
        unit_semantic=unit.attribution.semantic,
        canonical_provider_fragment=fragment,
        canonical_wire_utf8_sha256=f"sha256:{sha256(wire_bytes).hexdigest()}",
        canonical_wire_utf8_bytes=len(wire_bytes),
        canonical_content_digest=context_fingerprint(
            "provider-input-unit-canonical-content:v1",
            fragment.model_dump(mode="json"),
        ),
        lowering_contract_fingerprint=(
            unit.attribution.semantic.lowering_contract_fingerprint
        ),
        wire_codec_contract_fingerprint=(
            PROVIDER_UNIT_WIRE_CODEC_CONTRACT_FINGERPRINT
        ),
    )
    document_bytes = canonical_json_bytes(
        semantic_materialization.model_dump(mode="json")
    )
    document_identity = build_frozen_fact(
        ProviderInputUnitSemanticDocumentIdentityFact,
        schema_version="provider_input_unit_semantic_document_identity.v1",
        document_schema_version=(
            "provider_input_unit_semantic_materialization.v1"
        ),
        document_contract_fingerprint=(
            PROVIDER_UNIT_SEMANTIC_DOCUMENT_CONTRACT_FINGERPRINT
        ),
        semantic_materialization_fingerprint=(
            semantic_materialization.semantic_materialization_fingerprint
        ),
        canonical_document_sha256=(
            f"sha256:{sha256(document_bytes).hexdigest()}"
        ),
        canonical_document_bytes=len(document_bytes),
        canonical_wire_utf8_sha256=(
            semantic_materialization.canonical_wire_utf8_sha256
        ),
        canonical_wire_utf8_bytes=(
            semantic_materialization.canonical_wire_utf8_bytes
        ),
    )
    return semantic_materialization, document_identity


def freeze_provider_message_fragment(message: LLMMessage) -> ProviderMessageFragmentFact:
    """Freeze one exact provider-visible message without placement attribution."""

    blocks = []
    for text in message.content:
        blocks.append(
            build_frozen_fact(
                ProviderInputTextBlockFact,
                schema_version="provider_input_text_block.v1",
                text=text,
                utf8_bytes=len(text.encode("utf-8")),
            )
        )
    for thinking in message.thinking:
        blocks.append(
            build_frozen_fact(
                ProviderInputThinkingBlockFact,
                schema_version="provider_input_thinking_block.v1",
                text=thinking,
                utf8_bytes=len(thinking.encode("utf-8")),
            )
        )
    for call in message.tool_calls:
        blocks.append(_tool_call_block(call))
    if message.role is MessageRole.TOOL_CALL:
        if message.tool_call_id is None or message.name is None:
            raise ValueError("tool-call message lacks stable identity")
        blocks.append(
            _tool_call_block(
                LLMToolCall(
                    id=message.tool_call_id,
                    name=message.name,
                    arguments=message.arguments or "{}",
                )
            )
        )
    fragment = build_frozen_fact(
        ProviderMessageFragmentFact,
        schema_version="provider_message_fragment.v3",
        role=message.role.value,
        name=message.name,
        tool_call_id=message.tool_call_id,
        content_blocks=tuple(blocks),
        provider_user_carrier_binding=(
            message.provider_user_carrier_binding
        ),
    )
    return fragment


def freeze_ordered_transcript_unit(
    unit: ProviderOrderedTranscriptUnitFact,
    *,
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...],
    estimated_tokens: int,
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...] = (),
) -> ProviderInputUnitMaterializationFact:
    """Materialize one validated causal unit without invocation classification."""

    source_refs = ordered_transcript_unit_source_event_refs(unit)
    fragment = unit.wire_semantic.provider_message
    semantic = build_frozen_fact(
        ProviderInputUnitSemanticFact,
        schema_version="provider_input_unit_semantic.v3",
        unit_kind="transcript_message",
        provider_content_semantic_fingerprint=fragment.semantic_fingerprint,
        lowering_contract_id=LOWERING_CONTRACT_ID,
        lowering_contract_version=LOWERING_CONTRACT_VERSION,
        lowering_contract_fingerprint=LOWERING_CONTRACT_FINGERPRINT,
        pairing_group_id=None,
    )
    attribution = build_frozen_fact(
        ProviderInputUnitAttributionFact,
        schema_version="provider_input_unit_attribution.v1",
        semantic=semantic,
        owner_semantic_fingerprint=unit.unit_causal_semantic_fingerprint,
        source_event_refs=source_refs,
        source_artifact_refs=(),
        authority_horizons=authority_horizons,
        required_replay_bindings=required_replay_bindings,
    )
    return build_frozen_fact(
        ProviderInputUnitMaterializationFact,
        schema_version="provider_input_unit_materialization.v1",
        attribution=attribution,
        canonical_provider_fragment=fragment,
        estimated_tokens=max(0, estimated_tokens),
    )


def ordered_transcript_unit_source_event_refs(
    unit: ProviderOrderedTranscriptUnitFact,
) -> tuple[ContextEventReferenceFact, ...]:
    attribution = unit.source_attribution
    if isinstance(attribution, DirectStableMessageSourceAttributionFact):
        references = attribution.stable_leaf_reference.source_event_references
    elif isinstance(attribution, DerivedToolResultMessageSourceAttributionFact):
        references = (
            *attribution.tool_pair_leaf_reference.source_event_references,
            *attribution.tool_result_leaf_reference.source_event_references,
        )
    elif isinstance(attribution, CompactionReplacementSummarySourceAttributionFact):
        references = (
            *attribution.summary_leaf_reference.source_event_references,
            attribution.rewrite_authority_reference.compaction_completed_event_reference,
        )
    elif isinstance(attribution, LifecycleNoteSourceAttributionFact):
        references = (
            *attribution.note_leaf_reference.source_event_references,
            attribution.cause_event_reference,
            attribution.note_event_reference,
        )
    else:  # pragma: no cover - discriminated union is closed
        raise ValueError("unknown ordered transcript source attribution")
    by_key = {
        (item.runtime_session_id, item.sequence, item.event_id): item
        for item in references
    }
    return tuple(by_key[key] for key in sorted(by_key))


def freeze_tool_unit(
    tool: ToolSpec,
    *,
    authority_horizons: tuple[LedgerAuthorityHorizonFact, ...],
    estimated_tokens: int,
    source_event_refs: tuple[ContextEventReferenceFact, ...] = (),
    source_artifact_refs: tuple[ContextArtifactReferenceFact, ...] = (),
    required_replay_bindings: tuple[ProviderInputReplayBindingIdentityFact, ...] = (),
) -> ProviderInputUnitMaterializationFact:
    frozen_parameters = freeze_json(tool.parameters)
    from pulsara_agent.primitives._context_base import FrozenJsonObjectFact

    if not isinstance(frozen_parameters, FrozenJsonObjectFact):
        raise ValueError("provider tool parameters must be an object")
    fragment = build_frozen_fact(
        ProviderToolSpecFragmentFact,
        schema_version="provider_tool_spec_fragment.v1",
        name=tool.name,
        description=tool.description,
        frozen_parameters=frozen_parameters,
    )
    semantic = build_frozen_fact(
        ProviderInputUnitSemanticFact,
        schema_version="provider_input_unit_semantic.v3",
        unit_kind="tool_catalog",
        provider_content_semantic_fingerprint=fragment.semantic_fingerprint,
        lowering_contract_id=LOWERING_CONTRACT_ID,
        lowering_contract_version=LOWERING_CONTRACT_VERSION,
        lowering_contract_fingerprint=LOWERING_CONTRACT_FINGERPRINT,
        pairing_group_id=None,
    )
    attribution = build_frozen_fact(
        ProviderInputUnitAttributionFact,
        schema_version="provider_input_unit_attribution.v1",
        semantic=semantic,
        owner_semantic_fingerprint=fragment.semantic_fingerprint,
        source_event_refs=source_event_refs,
        source_artifact_refs=source_artifact_refs,
        authority_horizons=authority_horizons,
        required_replay_bindings=required_replay_bindings,
    )
    return build_frozen_fact(
        ProviderInputUnitMaterializationFact,
        schema_version="provider_input_unit_materialization.v1",
        attribution=attribution,
        canonical_provider_fragment=fragment,
        estimated_tokens=max(0, estimated_tokens),
    )


def hydrate_carrier(
    units: tuple[ProviderInputUnitMaterializationFact, ...],
) -> RecursivelyImmutableProviderInputCarrier:
    system_parts: list[str] = []
    messages: list[LLMMessage] = []
    tool_fragments: list[ProviderToolSpecFragmentFact] = []
    for unit in units:
        fragment = unit.canonical_provider_fragment
        if isinstance(fragment, ProviderToolSpecFragmentFact):
            # Keep the recursively frozen durable fragment in the carrier.
            # ToolSpec is only a dispatch DTO and must never become resident
            # authority.
            tool_fragments.append(fragment)
            continue
        message = _hydrate_message(fragment)
        if (
            message.role is MessageRole.SYSTEM
            and unit.attribution.semantic.unit_kind == "context_source"
        ):
            system_parts.extend(message.content)
        else:
            messages.append(message)
    system_prompt = "\n\n".join(system_parts) if system_parts else None
    accumulator = _extend_provider_content_accumulator(
        context_fingerprint("provider-input-carrier-content-genesis:v1", ()),
        tuple(
            unit.attribution.semantic.provider_content_semantic_fingerprint
            for unit in units
        ),
    )
    return _build_carrier(
        system_prompt=system_prompt,
        ordered_messages=tuple(messages),
        ordered_tool_fragments=tuple(tool_fragments),
        input_unit_count=len(units),
        ordered_provider_content_accumulator=accumulator,
    )


def append_carrier(
    previous: RecursivelyImmutableProviderInputCarrier,
    append_units: tuple[ProviderInputUnitMaterializationFact, ...],
) -> RecursivelyImmutableProviderInputCarrier:
    """Decode only newly appended fragments for a retained generation."""

    if not append_units:
        raise ValueError("provider carrier append cannot be empty")
    appended = hydrate_carrier(append_units)
    if appended.system_prompt is not None or appended.ordered_tool_fragments:
        raise ValueError("retained provider generation cannot append root fragments")
    accumulator = _extend_provider_content_accumulator(
        previous.ordered_provider_content_accumulator,
        tuple(
            unit.attribution.semantic.provider_content_semantic_fingerprint
            for unit in append_units
        ),
    )
    return _build_carrier(
        system_prompt=previous.system_prompt,
        ordered_messages=(*previous.ordered_messages, *appended.ordered_messages),
        ordered_tool_fragments=previous.ordered_tool_fragments,
        input_unit_count=previous.input_unit_count + len(append_units),
        ordered_provider_content_accumulator=accumulator,
    )


def _extend_provider_content_accumulator(
    predecessor: str,
    fingerprints: tuple[str, ...],
) -> str:
    accumulator = predecessor
    for fingerprint in fingerprints:
        accumulator = context_fingerprint(
            "provider-input-carrier-content-step:v1",
            (accumulator, fingerprint),
        )
    return accumulator


def _build_carrier(
    *,
    system_prompt: str | None,
    ordered_messages: tuple[LLMMessage, ...],
    ordered_tool_fragments: tuple[ProviderToolSpecFragmentFact, ...],
    input_unit_count: int,
    ordered_provider_content_accumulator: str,
) -> RecursivelyImmutableProviderInputCarrier:
    semantic = context_fingerprint(
        "provider-input-carrier-semantic:v2",
        {
            "input_unit_count": input_unit_count,
            "ordered_provider_content_accumulator": (
                ordered_provider_content_accumulator
            ),
        },
    )
    carrier_payload = {
        "input_unit_count": input_unit_count,
        "ordered_provider_content_accumulator": (ordered_provider_content_accumulator),
        "provider_input_semantic_fingerprint": semantic,
    }
    return RecursivelyImmutableProviderInputCarrier(
        system_prompt=system_prompt,
        ordered_messages=ordered_messages,
        ordered_tool_fragments=ordered_tool_fragments,
        input_unit_count=input_unit_count,
        ordered_provider_content_accumulator=(ordered_provider_content_accumulator),
        provider_input_semantic_fingerprint=semantic,
        carrier_fingerprint=context_fingerprint(
            "provider-input-carrier:v2", carrier_payload
        ),
    )


def validate_carrier_against_plan(
    *,
    carrier: RecursivelyImmutableProviderInputCarrier,
    plan: CanonicalProviderInputPlanFact,
) -> None:
    """Join one hydrated immutable carrier to its event-safe semantic plan."""

    identity = plan.provider_input_semantic_identity
    if (
        context_fingerprint("provider-input-system-prompt:v1", carrier.system_prompt)
        != identity.system_instruction_fingerprint
    ):
        raise ValueError("provider input system instruction hydration drifted")
    if (
        context_fingerprint(
            "provider-input-tool-catalog:v1",
            tuple(
                tool_fragment_semantic_fingerprint(item)
                for item in carrier.ordered_tool_fragments
            ),
        )
        != identity.tool_catalog_fingerprint
    ):
        raise ValueError("provider input tool catalog hydration drifted")
    if (
        context_fingerprint(
            "provider-input-message-sequence:v1",
            tuple(
                message_semantic_fingerprint(item) for item in carrier.ordered_messages
            ),
        )
        != identity.provider_message_sequence_fingerprint
    ):
        raise ValueError("provider input message sequence hydration drifted")


def validate_dispatch_context_against_plan(
    *,
    context: LLMContext,
    plan: CanonicalProviderInputPlanFact,
) -> None:
    """Recompute the actual pre-send wire identity from the dispatch graph."""

    identity = plan.provider_input_semantic_identity
    if (
        context_fingerprint("provider-input-system-prompt:v1", context.system_prompt)
        != identity.system_instruction_fingerprint
    ):
        raise ValueError("provider input dispatch system instruction drifted")
    if (
        context_fingerprint(
            "provider-input-tool-catalog:v1",
            tuple(tool_semantic_fingerprint(item) for item in context.tools),
        )
        != identity.tool_catalog_fingerprint
    ):
        raise ValueError("provider input dispatch tool catalog drifted")
    if (
        context_fingerprint(
            "provider-input-message-sequence:v1",
            tuple(message_semantic_fingerprint(item) for item in context.messages),
        )
        != identity.provider_message_sequence_fingerprint
    ):
        raise ValueError("provider input dispatch message sequence drifted")


def message_semantic_fingerprint(message: LLMMessage) -> str:
    return context_fingerprint(
        "provider-message-wire-semantic:v1", _message_payload(message)
    )


def tool_semantic_fingerprint(tool: ToolSpec) -> str:
    return context_fingerprint(
        "provider-tool-wire-semantic:v1",
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    )


def tool_fragment_semantic_fingerprint(
    fragment: ProviderToolSpecFragmentFact,
) -> str:
    thawed = thaw_json(fragment.frozen_parameters)
    if not isinstance(thawed, dict):
        raise ValueError("provider tool schema did not thaw to object")
    return context_fingerprint(
        "provider-tool-wire-semantic:v1",
        {
            "name": fragment.name,
            "description": fragment.description,
            "parameters": thawed,
        },
    )


def _tool_call_block(call: LLMToolCall) -> ProviderInputToolCallBlockFact:
    try:
        parsed = json.loads(call.arguments)
    except json.JSONDecodeError:
        state = "invalid_json"
        frozen = None
        error = "invalid_json"
    else:
        if isinstance(parsed, dict):
            from pulsara_agent.primitives._context_base import FrozenJsonObjectFact

            candidate = freeze_json(parsed)
            assert isinstance(candidate, FrozenJsonObjectFact)
            state = "valid_object"
            frozen = candidate
            error = None
        else:
            state = "non_object_json"
            frozen = None
            error = "non_object_json"
    return build_frozen_fact(
        ProviderInputToolCallBlockFact,
        schema_version="provider_input_tool_call_block.v1",
        tool_call_id=call.id,
        model_tool_name=call.name,
        arguments_state=state,
        canonical_arguments=frozen,
        raw_arguments_json=call.arguments,
        parse_error_code=error,
    )


def _hydrate_message(fragment: ProviderMessageFragmentFact) -> LLMMessage:
    content: list[str] = []
    thinking: list[str] = []
    tool_calls: list[LLMToolCall] = []
    for block in fragment.content_blocks:
        if isinstance(block, ProviderInputTextBlockFact):
            content.append(block.text)
        elif isinstance(block, ProviderInputThinkingBlockFact):
            thinking.append(block.text)
        elif isinstance(block, ProviderInputToolCallBlockFact):
            tool_calls.append(
                LLMToolCall(
                    id=block.tool_call_id,
                    name=block.model_tool_name,
                    arguments=block.raw_arguments_json,
                )
            )
        else:
            raise ValueError("unsupported provider message block in V1 carrier")
    if fragment.role == MessageRole.TOOL_CALL.value:
        if len(tool_calls) != 1:
            raise ValueError("provider tool-call fragment requires exactly one call")
        call = tool_calls[0]
        return LLMMessage(
            role=MessageRole.TOOL_CALL,
            content=tuple(content),
            thinking=tuple(thinking),
            tool_call_id=call.id,
            name=call.name,
            arguments=call.arguments,
        )
    carrier_semantic = None
    if fragment.provider_user_carrier_binding is not None:
        from pulsara_agent.llm.user_carrier import (
            rebind_provider_user_carrier_semantic,
        )

        if len(content) != 1:
            raise ValueError("provider user carrier fragment requires one text block")
        carrier_semantic = rebind_provider_user_carrier_semantic(
            content[0],
            binding=fragment.provider_user_carrier_binding,
        )
    return LLMMessage(
        role=MessageRole(fragment.role),
        content=tuple(content),
        thinking=tuple(thinking),
        tool_calls=tuple(tool_calls),
        tool_call_id=fragment.tool_call_id,
        name=fragment.name,
        arguments=None,
        provider_user_carrier_semantic=carrier_semantic,
        provider_user_carrier_binding=fragment.provider_user_carrier_binding,
    )


def _message_payload(message: LLMMessage) -> dict[str, object]:
    return {
        "role": message.role.value,
        "content": message.content,
        "thinking": message.thinking,
        "tool_calls": tuple(
            {"id": item.id, "name": item.name, "arguments": item.arguments}
            for item in message.tool_calls
        ),
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "arguments": message.arguments,
    }


def _deep_copy_json_object(value: dict[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _hydrate_tool_fragment(fragment: ProviderToolSpecFragmentFact) -> ToolSpec:
    thawed = thaw_json(fragment.frozen_parameters)
    if not isinstance(thawed, dict):
        raise ValueError("provider tool schema did not thaw to object")
    return ToolSpec(
        name=fragment.name,
        description=fragment.description,
        parameters=_deep_copy_json_object(thawed),
    )


__all__ = [
    "LOWERING_CONTRACT_FINGERPRINT",
    "PROVIDER_UNIT_SEMANTIC_DOCUMENT_CONTRACT_FINGERPRINT",
    "PROVIDER_UNIT_WIRE_CODEC_CONTRACT_FINGERPRINT",
    "RecursivelyImmutableProviderInputCarrier",
    "build_provider_unit_semantic_document",
    "freeze_message_unit",
    "freeze_tool_unit",
    "hydrate_carrier",
    "validate_carrier_against_plan",
    "validate_dispatch_context_against_plan",
    "message_semantic_fingerprint",
    "tool_fragment_semantic_fingerprint",
    "tool_semantic_fingerprint",
]
