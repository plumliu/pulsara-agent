"""Optional AgentRuntime persistence hooks for memory ledger ingestion."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.foundation.provenance import RuntimeEventSpan
from pulsara_agent.message import ToolResultBlock
from pulsara_agent.runtime.state import LoopState


@dataclass(slots=True)
class ExecutionEvidencePersistenceHook:
    """Persist tool result runtime facts without promoting semantic claims."""

    ledger: ExecutionEvidenceLedger
    scope: str | None = None

    async def after_tool_results(self, state: LoopState, results: list[ToolResultBlock]) -> None:
        spans = state.scratchpad.get("tool_result_event_spans", {})
        inputs = {call.id: call.input for call in state.pending_tool_calls}
        scope = self.scope or state.current_scope or f"ctx:{state.turn_id}"
        for result in results:
            span = spans.get(result.id)
            if span is not None and not isinstance(span, RuntimeEventSpan):
                span = None
            self.ledger.record_tool_result_block(
                turn_id=state.turn_id,
                block=result,
                input_summary=inputs.get(result.id, ""),
                scope=scope,
                event_span=span,
            )
