"""Pure builders for canonical transcript message semantics.

The committed transcript reducer and same-batch provider-input preparation must
derive byte-identical message semantic identities.  Keeping this builder below
both runtime packages prevents either path from becoming an implicit second
semantic owner.
"""

from __future__ import annotations

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.transcript_projection import (
    TranscriptMessageLeafSemanticFact,
    TranscriptMessageProviderPlacementSemanticFact,
    TranscriptMessageProviderSemanticFact,
    TranscriptProviderTextBlockSemanticFact,
)


def build_transcript_message_provider_semantic(
    *,
    role: str,
    name: str | None,
    segment: str,
    ordered_block_fingerprints: tuple[str, ...],
) -> TranscriptMessageProviderSemanticFact:
    """Build the sole provider-visible semantic identity for a transcript message."""

    if segment == "current_user":
        lane = "current_user"
        scope = "leading_user"
        timing = "current_user"
    elif segment == "current_run_tail":
        lane = "current_run_tail"
        scope = "transcript_current_run"
        timing = "current_run_observation"
    else:
        lane = "prior_history"
        scope = "transcript_prior"
        timing = "historical_replay"
    placement = build_frozen_fact(
        TranscriptMessageProviderPlacementSemanticFact,
        schema_version="transcript_message_provider_placement_semantic.v2",
        normalized_lane=lane,
        lowering_scope=scope,
        timing_overlay_kind=timing,
        timing_policy_semantic_fingerprint=context_fingerprint(
            "transcript-timing-policy-semantic:v1", timing
        ),
        placement_contract_id="pulsara.transcript-message-placement",
        placement_contract_version="2",
        placement_contract_fingerprint=context_fingerprint(
            "transcript-message-placement-contract:v2",
            "current-user+current-run-tail+prior-history",
        ),
    )
    return build_frozen_fact(
        TranscriptMessageProviderSemanticFact,
        schema_version="transcript_message_provider_semantic.v4",
        role=role,
        name=name,
        placement_semantic=placement,
        ordered_block_semantic_fingerprints=ordered_block_fingerprints,
    )


def build_inline_text_message_semantics(
    *,
    text: str,
    role: str,
    name: str | None,
    segment: str,
) -> tuple[
    TranscriptProviderTextBlockSemanticFact,
    TranscriptMessageProviderSemanticFact,
    TranscriptMessageLeafSemanticFact,
]:
    """Build the semantic layers shared by live fold and same-batch steer."""

    block = build_frozen_fact(
        TranscriptProviderTextBlockSemanticFact,
        schema_version="transcript_provider_text_block_semantic.v1",
        block_kind="text",
        text=text,
    )
    provider = build_transcript_message_provider_semantic(
        role=role,
        name=name,
        segment=segment,
        ordered_block_fingerprints=(block.semantic_fingerprint,),
    )
    leaf = build_frozen_fact(
        TranscriptMessageLeafSemanticFact,
        schema_version="transcript_message_leaf_semantic.v2",
        semantic_kind="message",
        message_provider_semantic_identity=provider,
    )
    return block, provider, leaf


__all__ = [
    "build_inline_text_message_semantics",
    "build_transcript_message_provider_semantic",
]
