"""Process-local ownership vocabulary for MCP subscription invalidation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.primitives.mcp_protocol import McpSnapshotDirtyReason


McpSubscriptionDirtyKind = Literal["tools", "prompts", "resources"]


@dataclass(frozen=True, slots=True)
class McpServerDirtySignal:
    """Exact installed-generation signal emitted after its dispatch barrier."""

    server_id: str
    snapshot_id: str
    config_epoch: int
    discovery_generation: int
    transport_generation: int
    signal_generation: int
    dirty_reasons: tuple[McpSnapshotDirtyReason, ...]
    dirty_kinds: tuple[McpSubscriptionDirtyKind, ...]
    observed_monotonic: float

    def __post_init__(self) -> None:
        if not self.server_id or not self.snapshot_id:
            raise ValueError("MCP dirty signal identity is required")
        if min(
            self.config_epoch,
            self.discovery_generation,
            self.transport_generation,
            self.signal_generation,
        ) < 0:
            raise ValueError("MCP dirty signal generations must be non-negative")
        if not self.dirty_reasons:
            raise ValueError("MCP dirty signal requires at least one reason")
        if self.dirty_reasons != tuple(
            sorted(set(self.dirty_reasons), key=lambda item: item.value)
        ):
            raise ValueError("MCP dirty reasons must be ordered and unique")
        if self.dirty_kinds != tuple(sorted(set(self.dirty_kinds))):
            raise ValueError("MCP dirty kinds must be ordered and unique")
        if self.observed_monotonic < 0:
            raise ValueError("MCP dirty signal monotonic time must be non-negative")


__all__ = [
    "McpServerDirtySignal",
    "McpSnapshotDirtyReason",
    "McpSubscriptionDirtyKind",
]
