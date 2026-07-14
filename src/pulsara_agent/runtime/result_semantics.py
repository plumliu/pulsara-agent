"""Stable semantic identity for committed terminal tool results."""

from __future__ import annotations

from hashlib import sha256
from typing import Iterable

from pulsara_agent.event import (
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.primitives._context_base import context_fingerprint


class ToolResultSemanticContractError(RuntimeError):
    """A terminal result cannot be reduced to one typed semantic outcome."""


def typed_terminal_result_semantic_fingerprint(
    *,
    terminal: ToolResultEndEvent,
    semantic_events: Iterable[ToolResultTextDeltaEvent | ToolResultDataDeltaEvent],
) -> str:
    """Fingerprint result semantics without volatile event or timing attribution.

    Text chunks are joined before hashing so transport chunking cannot change the
    identity. Data payloads are represented by content digests; the original bytes,
    URLs, event IDs, sequences, and observation timestamps never enter the result.
    """

    ordered = tuple(sorted(semantic_events, key=_stored_sequence))
    text_parts: list[str] = []
    data_parts: list[dict[str, object]] = []
    for event in ordered:
        if event.tool_call_id != terminal.tool_call_id:
            raise ToolResultSemanticContractError(
                "tool-result semantic event belongs to another call"
            )
        if _stored_sequence(event) >= _stored_sequence(terminal):
            raise ToolResultSemanticContractError(
                "tool-result semantic event must precede terminal result"
            )
        if isinstance(event, ToolResultTextDeltaEvent):
            text_parts.append(event.delta)
            continue
        source_kind = "data" if event.data is not None else "url"
        source_value = event.data if event.data is not None else event.url
        assert source_value is not None
        data_parts.append(
            {
                "media_type": event.media_type,
                "source_kind": source_kind,
                "source_sha256": f"sha256:{sha256(source_value.encode('utf-8')).hexdigest()}",
            }
        )

    profile = terminal.render_profile
    attribution = profile.descriptor_attribution
    payload = {
        "result_state": terminal.state.value,
        "text_sha256": f"sha256:{sha256(''.join(text_parts).encode('utf-8')).hexdigest()}",
        "data_parts": tuple(data_parts),
        "artifacts": tuple(
            artifact.model_dump(mode="json") for artifact in terminal.artifacts
        ),
        "render_contract_fingerprint": profile.render_contract_fingerprint,
        "render_variant_fingerprint": (
            profile.selected_variant.variant_fingerprint
        ),
        "tool_origin": profile.tool_origin,
        "descriptor_semantics": (
            None
            if attribution is None
            else {
                "descriptor_id": attribution.descriptor_id,
                "descriptor_fingerprint": attribution.descriptor_fingerprint,
                "result_render_contract_fingerprint": (
                    attribution.result_render_contract_fingerprint
                ),
            }
        ),
        "essential_result": (
            terminal.essential_result.model_dump(mode="json")
            if terminal.essential_result is not None
            else None
        ),
    }
    return context_fingerprint("typed-terminal-tool-result:v1", payload)


def _stored_sequence(event: object) -> int:
    sequence = getattr(event, "sequence", None)
    if not isinstance(sequence, int) or sequence < 1:
        raise ToolResultSemanticContractError(
            "typed terminal result semantics require committed events"
        )
    return sequence


__all__ = [
    "ToolResultSemanticContractError",
    "typed_terminal_result_semantic_fingerprint",
]
