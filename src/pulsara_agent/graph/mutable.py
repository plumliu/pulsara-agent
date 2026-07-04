"""Narrow mutation protocol for canonical memory lifecycle changes."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pulsara_agent.ontology import memory


class MutableCanonicalMemoryStore(Protocol):
    def set_status(
        self,
        node_id: str,
        status: memory.NodeStatus,
        *,
        updated_at: datetime,
        graph_id: str | None = None,
    ) -> None:
        """Update one existing canonical memory node status."""
