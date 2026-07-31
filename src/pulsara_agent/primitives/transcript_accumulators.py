"""Pure genesis identities for transcript and ledger prefix accumulators."""

from pulsara_agent.primitives.context import context_fingerprint


EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR = context_fingerprint(
    "transcript-prefix-accumulator:v1",
    "empty",
)
EMPTY_LEDGER_CONTINUITY_ACCUMULATOR = context_fingerprint(
    "ledger-continuity-accumulator:v1",
    "empty",
)


__all__ = [
    "EMPTY_LEDGER_CONTINUITY_ACCUMULATOR",
    "EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR",
]
