"""Pure materialization of one accepted model continuation."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping

from pulsara_agent.llm.input import LLMMessage, LLMToolCall
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.provider_input import ProviderInputPendingContinuationFact
from pulsara_agent.primitives.terminal_projection import (
    ModelDataBlockSemanticFact,
    ModelProviderErrorSemanticFact,
    ModelTerminalProjectionPayloadFact,
    ModelTextBlockSemanticFact,
    ModelThinkingBlockSemanticFact,
    ModelToolCallBlockSemanticFact,
    TerminalArtifactContentReferenceFact,
    TerminalContentFact,
    TerminalInlineContentFact,
    TerminalProjectionDocumentFact,
)
from pulsara_agent.primitives.transcript_projection import (
    TerminalProjectionMessageContentRefFact,
    TranscriptMessageLeafEntryFact,
    TranscriptProjectionLeafEntryFact,
)
from pulsara_agent.runtime.provider_input.materialization import (
    message_semantic_fingerprint,
)


@dataclass(frozen=True, slots=True)
class PreparedProviderInputContinuationMaterialization:
    pending_continuation_fingerprint: str
    terminal_projection_reference_fingerprint: str
    terminal_document_fact_fingerprint: str
    provider_message: LLMMessage
    provider_message_semantic_fingerprint: str
    owner_semantic_fingerprint: str
    reply_id: str
    matched_stable_entry_fact_fingerprint: str
    matched_stable_entry_semantic_fingerprint: str


def required_continuation_content_artifacts(
    document: TerminalProjectionDocumentFact,
) -> tuple[TerminalArtifactContentReferenceFact, ...]:
    """Return the immutable terminal-content documents needed for validation."""

    if not isinstance(document.payload, ModelTerminalProjectionPayloadFact):
        raise ValueError("provider continuation requires a model terminal document")
    references: dict[str, TerminalArtifactContentReferenceFact] = {}
    for item in document.payload.items:
        content = item.content
        if not isinstance(content, TerminalArtifactContentReferenceFact):
            continue
        existing = references.get(content.artifact_id)
        if existing is not None and existing != content:
            raise ValueError("provider continuation content artifact identity conflict")
        references[content.artifact_id] = content
    return tuple(references[key] for key in sorted(references))


def prepare_provider_input_continuation(
    *,
    pending: ProviderInputPendingContinuationFact,
    document: TerminalProjectionDocumentFact,
    terminal_content_texts: Mapping[str, str],
    stable_entries: tuple[TranscriptProjectionLeafEntryFact, ...],
) -> PreparedProviderInputContinuationMaterialization:
    """Lower the exact accepted terminal projection into one assistant message."""

    reference = pending.terminal_projection_reference
    if (
        reference.projection_kind != "model_call"
        or document.fact_fingerprint != reference.document_fact_fingerprint
        or document.semantic_identity.semantic_fingerprint
        != reference.semantic_join.semantic_fingerprint
        or not isinstance(document.payload, ModelTerminalProjectionPayloadFact)
        or document.semantic_identity.terminal_outcome != "completed"
        or document.source_fact.resolved_model_call_id != pending.resolved_model_call_id
    ):
        raise ValueError("pending continuation terminal projection join failed")

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []
    expected_order = 0
    for item in document.payload.items:
        semantic = item.semantic_identity
        if semantic.projection_order != expected_order:
            raise ValueError("terminal continuation projection order is not contiguous")
        expected_order += 1
        if isinstance(semantic, ModelProviderErrorSemanticFact):
            raise ValueError("completed continuation cannot contain provider error")
        if getattr(semantic, "completion_status", "completed") != "completed":
            raise ValueError("interrupted model block cannot enter continuation")
        if isinstance(semantic, ModelTextBlockSemanticFact):
            text_parts.append(
                _terminal_content_text(item.content, terminal_content_texts)
            )
        elif isinstance(semantic, ModelThinkingBlockSemanticFact):
            thinking_parts.append(
                _terminal_content_text(item.content, terminal_content_texts)
            )
        elif isinstance(semantic, ModelDataBlockSemanticFact):
            name = ""
            text_parts.append(
                f"[data block omitted id={semantic.block_id}{name} "
                f"media_type={semantic.media_type} source=terminal_projection]"
            )
        elif isinstance(semantic, ModelToolCallBlockSemanticFact):
            tool_calls.append(
                LLMToolCall(
                    id=semantic.tool_call_id,
                    name=semantic.tool_name,
                    arguments=semantic.raw_arguments_json,
                )
            )
        else:  # pragma: no cover - the discriminated union is closed
            raise ValueError("unsupported terminal continuation item")

    message = LLMMessage.assistant_turn(
        text="\n".join(text_parts),
        thinking=tuple(thinking_parts),
        tool_calls=tuple(tool_calls),
    )
    message_fingerprint = message_semantic_fingerprint(message)
    matches = tuple(
        entry
        for entry in stable_entries
        if isinstance(entry, TranscriptMessageLeafEntryFact)
        and isinstance(entry.content, TerminalProjectionMessageContentRefFact)
        and entry.content.projection_reference == reference
    )
    if len(matches) != 1 or matches[0].attribution.reply_id is None:
        raise ValueError(
            "pending continuation does not join one exact stable assistant leaf"
        )
    matched = matches[0]
    owner_fingerprint = context_fingerprint(
        "provider-input-continuation-unit-owner:v1",
        {
            "pending_continuation_fingerprint": pending.continuation_fingerprint,
            "terminal_projection_reference_fingerprint": reference.reference_fingerprint,
            "provider_message_semantic_fingerprint": message_fingerprint,
        },
    )
    return PreparedProviderInputContinuationMaterialization(
        pending_continuation_fingerprint=pending.continuation_fingerprint,
        terminal_projection_reference_fingerprint=reference.reference_fingerprint,
        terminal_document_fact_fingerprint=document.fact_fingerprint,
        provider_message=message,
        provider_message_semantic_fingerprint=message_fingerprint,
        owner_semantic_fingerprint=owner_fingerprint,
        reply_id=matched.attribution.reply_id,
        matched_stable_entry_fact_fingerprint=matched.fact_fingerprint,
        matched_stable_entry_semantic_fingerprint=(
            matched.semantic_identity.semantic_fingerprint
        ),
    )


def _terminal_content_text(
    content: TerminalContentFact | None,
    terminal_content_texts: Mapping[str, str],
) -> str:
    if isinstance(content, TerminalInlineContentFact):
        return content.text
    if isinstance(content, TerminalArtifactContentReferenceFact):
        try:
            text = terminal_content_texts[content.artifact_id]
        except KeyError as exc:
            raise ValueError("terminal continuation content was not hydrated") from exc
        encoded = text.encode("utf-8")
        if (
            len(encoded) != content.artifact_bytes
            or f"sha256:{sha256(encoded).hexdigest()}" != content.artifact_sha256
        ):
            raise ValueError("terminal continuation content artifact drifted")
        return text
    raise ValueError("terminal continuation content is missing")


__all__ = [
    "PreparedProviderInputContinuationMaterialization",
    "prepare_provider_input_continuation",
    "required_continuation_content_artifacts",
]
