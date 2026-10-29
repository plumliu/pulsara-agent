"""Real tool-call provenance, independent of physical attempts and provider views."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from pulsara_agent.identity import derive_id
from pulsara_agent.primitives.context import FrozenJsonObjectFact


class ToolInvocationSourceKind(StrEnum):
    MODEL_TOOL_CALL = "MODEL_TOOL_CALL"
    CODE_MODE_CALL = "CODE_MODE_CALL"


@dataclass(frozen=True, slots=True)
class ToolInvocationSource:
    invocation_id: str
    kind: ToolInvocationSourceKind
    turn_id: str
    assistant_entry_id: str | None = None
    tool_call_id: str | None = None
    source_exec_attempt_id: str | None = None
    call_sequence: int | None = None
    tool_name: str | None = None
    original_arguments: FrozenJsonObjectFact | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ToolInvocationSourceKind):
            raise TypeError("tool invocation source kind must be closed")
        model = self.kind is ToolInvocationSourceKind.MODEL_TOOL_CALL
        if not self.invocation_id or not self.turn_id:
            raise ValueError("tool invocation identity is required")
        if model:
            if not self.assistant_entry_id or not self.tool_call_id or any(
                value is not None for value in (self.source_exec_attempt_id,
                    self.call_sequence, self.tool_name, self.original_arguments)
            ):
                raise ValueError("model invocation must reference its real tool-call block")
        elif (self.assistant_entry_id is not None or self.tool_call_id is not None
              or not self.source_exec_attempt_id or not self.tool_name
              or isinstance(self.call_sequence, bool) or not isinstance(self.call_sequence, int)
              or self.call_sequence < 1
              or not isinstance(self.original_arguments, FrozenJsonObjectFact)):
            raise ValueError("code invocation must carry its actual program source")

    @classmethod
    def model(cls, session_id: str, turn_id: str, entry_id: str, call_id: str):
        return cls(derive_id("call", "model-invocation", session_id, entry_id, call_id),
                   ToolInvocationSourceKind.MODEL_TOOL_CALL, turn_id, entry_id, call_id)


# The executor installs provenance around a single leaf call. asyncio.to_thread
# copies this context into canonical IO, so concurrent calls cannot share it.
current_tool_invocation: ContextVar[ToolInvocationSource | None] = ContextVar(
    "current_tool_invocation", default=None
)
