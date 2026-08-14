"""Process-local admission hooks for the single Host interaction arbiter.

The callbacks are opaque capabilities, not durable facts.  MCP uses them to
obtain an exact generation-bound dispatch permit only when its dormant
confirmation reaches the visible FIFO head.  Ordinary confirmations use the
same arbiter with no hooks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


MAXIMUM_DORMANT_INTERACTION_CANDIDATES = 64


@dataclass(frozen=True, slots=True)
class InteractionAdmissionHooks:
    before_publish: Callable[[], None]
    discard: Callable[[], None]
    owner_key: str | None = None


__all__ = [
    "InteractionAdmissionHooks",
    "MAXIMUM_DORMANT_INTERACTION_CANDIDATES",
]
