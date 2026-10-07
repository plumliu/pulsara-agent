"""Historical encoding at existing canonical ID boundaries, never a budget API.

Subagent acceptance and steer interruption IDs include this old preimage. Only
identity builders may use these private scalars; callers receive a digest, not
a token quote. See the unified final-input budget specification, section 6.4.
"""

from dataclasses import fields

from pulsara_agent.llm.estimator import (
    PulsaraHeuristicTokenEstimatorV3,
    estimate_image_visual_tokens,
)
from pulsara_agent.llm.input import LLMImagePart, LLMMessage, LLMTextPart
from pulsara_agent.primitives.context import context_fingerprint, thaw_json


def _message_code(message):
    primitive = PulsaraHeuristicTokenEstimatorV3()
    value = 4
    for part in message.content:
        if isinstance(part, LLMImagePart):
            value += estimate_image_visual_tokens(width=part.width, height=part.height)
        elif isinstance(part, LLMTextPart):
            value += primitive.estimate_text(part.text)
        else:
            raise TypeError("invalid historical message identity part")
    for call in message.tool_calls:
        value += 4 + sum(
            primitive.estimate_text(v) for v in (call.id, call.name, call.arguments)
        )
    return value + sum(
        primitive.estimate_text(v)
        for v in (message.tool_call_id, message.name, message.arguments)
        if v is not None
    )


def _input_code(system_prompt, messages, tools, checkpoint=lambda: None):
    primitive = PulsaraHeuristicTokenEstimatorV3()
    system = 4 + primitive.estimate_text(system_prompt) if system_prompt else 0
    per_message = []
    for message in messages:
        checkpoint()
        per_message.append(_message_code(message))
    tool = 0
    for item in tools:
        checkpoint()
        tool += 8 + primitive.estimate_json(
            {
                "name": item.name,
                "description": item.description,
                "parameters": thaw_json(item.parameters),
            }
        )
    return dict(
        system_tokens=system,
        message_tokens=sum(per_message),
        message_tokens_by_index=tuple(per_message),
        tool_tokens=tool,
        envelope_tokens=3,
        visual_image_tokens=sum(
            estimate_image_visual_tokens(width=p.width, height=p.height)
            for m in messages
            for p in m.content
            if isinstance(p, LLMImagePart)
        ),
        total_input_tokens=system + sum(per_message) + tool + 3,
    )


def compiled_input_identity(
    *,
    context_id,
    canonical_input_identity,
    system_prompt,
    messages,
    tools,
    source_decisions,
    tool_result_decisions,
    compile_report,
    diagnostic_codes,
    source_collection_fingerprint,
    compile_binding_fingerprint,
    estimator_fingerprint,
    effective_input_budget_tokens,
    source_contents,
    tool_messages,
    protected_messages=(),
    context_messages=None,
    checkpoint=lambda: None,
):
    """Preserve the existing compiled digest used by canonical acceptance IDs."""
    from pulsara_agent.model_input.contracts import _llm_message_value

    encoded = _input_code(system_prompt, messages, tools, checkpoint)
    primitive = PulsaraHeuristicTokenEstimatorV3()
    source_codes = []
    system_fragments = []
    for content in source_contents:
        checkpoint()
        if content is None:
            source_codes.append(0)
        elif isinstance(content, str):
            before = primitive.estimate_text("\n\n".join(system_fragments))
            system_fragments.append(content)
            source_codes.append(
                primitive.estimate_text("\n\n".join(system_fragments)) - before
            )
        elif isinstance(content, LLMMessage):
            source_codes.append(_message_code(content))
        else:
            raise TypeError("invalid source identity content")
    tool_codes = tuple(_message_code(m) for m in tool_messages)
    report = {
        field.name: getattr(compile_report, field.name)
        for field in fields(compile_report)
    }
    report.update(
        estimator_fingerprint=estimator_fingerprint,
        effective_input_budget_tokens=effective_input_budget_tokens,
        **{
            key: encoded[key]
            for key in (
                "system_tokens",
                "message_tokens",
                "tool_tokens",
                "envelope_tokens",
                "total_input_tokens",
            )
        },
        protected_transcript_tokens=sum(_message_code(m) for m in protected_messages),
        context_source_tokens=(
            encoded["system_tokens"]
            + sum(
                _message_code(m) for m in source_contents if isinstance(m, LLMMessage)
            )
            if context_messages is None
            else sum(_message_code(m) for m in context_messages)
        ),
    )
    return context_fingerprint(
        "frozen-compiled-model-input:v1",
        {
            "context_id": context_id,
            "canonical_identity": canonical_input_identity.identity_fingerprint,
            "compile_binding": compile_binding_fingerprint,
            "source_collection": source_collection_fingerprint,
            "system_prompt": system_prompt,
            "messages": tuple(_llm_message_value(item) for item in messages),
            "tools": tuple(tool.canonical_bytes.decode("utf-8") for tool in tools),
            "estimate": encoded,
            "source_decisions": tuple(
                (
                    d.source_kind.value,
                    d.source_instance_fingerprint,
                    d.channel.value,
                    None if d.selected_mode is None else d.selected_mode.value,
                    d.included,
                    cost,
                    d.reason_code,
                )
                for d, cost in zip(source_decisions, source_codes, strict=True)
            ),
            "tool_result_decisions": tuple(
                (
                    d.source_entry_fingerprint,
                    d.current_turn,
                    d.first_legal_mode.value,
                    d.selected_mode.value,
                    d.delivery_requirement.value,
                    None
                    if d.full_delivery_reason is None
                    else d.full_delivery_reason.value,
                    cost,
                    d.reason_code,
                )
                for d, cost in zip(tool_result_decisions, tool_codes, strict=True)
            ),
            "budget_report": report,
            "diagnostics": tuple(item.value for item in diagnostic_codes),
        },
    )


def memory_reservation_identity(
    *,
    source_kind,
    prior,
    desired_presence,
    desired_fingerprint,
    contract,
    invalidations,
    full_message,
    epoch_bytes,
    estimator_fingerprint,
):
    """Return only the existing reservation digest needed by a stable steer ID."""
    from pulsara_agent.model_input.continuity import provider_input_logical_bytes

    return context_fingerprint(
        "pulsara:memory-source-invalidation-reservation:v1",
        {
            "source": source_kind.value,
            "prior": (prior.presence.value, prior.semantic_fingerprint),
            "desired": (desired_presence.value, desired_fingerprint),
            "contract": contract,
            "invalidation": (
                1,
                epoch_bytes,
                max(_message_code(m) for m in invalidations),
                epoch_bytes,
            ),
            "full": (
                0
                if full_message is None
                else provider_input_logical_bytes(
                    system_prompt="", tools=(), messages=(full_message,)
                ),
                0 if full_message is None else _message_code(full_message),
            ),
            "estimator": estimator_fingerprint,
        },
    )


def steer_quote_identity(*, candidate_ids, quote, compiled):
    """Encode the historical quote only while deriving its canonical plan ID."""
    encoded = _input_code(compiled.system_prompt, compiled.messages, compiled.tools)
    historical = {
        key: encoded[value]
        for key, value in {
            "system": "system_tokens",
            "messages": "message_tokens",
            "message_by_index": "message_tokens_by_index",
            "tools": "tool_tokens",
            "envelope": "envelope_tokens",
            "visual_image": "visual_image_tokens",
            "total": "total_input_tokens",
        }.items()
    }
    return context_fingerprint(
        "pulsara:steer-suffix-admission-quote:v1",
        {
            "candidates": candidate_ids,
            "selected_items": quote.selected_item_count,
            "selected_canonical_bytes": quote.selected_canonical_expanded_bytes,
            "snapshot_bytes": quote.prospective_snapshot_hydrated_bytes,
            "epoch_bytes": quote.resulting_epoch_logical_bytes,
            "estimate": historical,
            "effective_budget": quote.effective_target_budget,
            "estimator": quote.estimator_fingerprint,
            "predecessor_prefix": quote.predecessor_prefix_fingerprint,
            "memory_recall_reservation": None
            if quote.memory_recall_reservation is None
            else quote.memory_recall_reservation.stable_identity,
            "memory_response_preference_reservation": None
            if quote.memory_response_preference_reservation is None
            else quote.memory_response_preference_reservation.stable_identity,
        },
    )
