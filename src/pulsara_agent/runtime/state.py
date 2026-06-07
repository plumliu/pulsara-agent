"""Loop state for the main agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LoopState:
    """Short-lived state for one active agent loop.

    This is the Working Context Cache. It is not a durable fact source.
    """

    session_id: str
    turn_index: int = 0
    current_scope: str | None = None
    memory_projection: dict[str, Any] | None = None
    scratchpad: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, int] = field(default_factory=dict)
