"""Frozen source facts carried by an accepted ROOT tool call."""

from dataclasses import dataclass, field
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot


@dataclass(frozen=True, slots=True)
class FrozenAgentInvocationSnapshot:
    session_id: str
    turn_id: str
    model_call_binding: ModelCallBinding
    permission_snapshot: FrozenRunPermissionSnapshot
    initial_context_units: tuple[str, ...] = field(repr=False)
