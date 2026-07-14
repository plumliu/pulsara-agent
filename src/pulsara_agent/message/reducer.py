"""Replay AgentEvent streams into runtime messages."""

from __future__ import annotations

from pulsara_agent.event.events import (
    AgentEvent,
    EventType,
    ExternalExecutionResultEvent,
    ModelCallControlDispositionResolvedEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    ToolResultEndEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.primitives.model_call import ModelCallControlDisposition
from pulsara_agent.message.assembler import BlockAssembler
from pulsara_agent.message.blocks import (
    ToolCallBlock,
    ToolCallState,
)
from pulsara_agent.message.message import Msg, Usage


class MessageReducer:
    """Rebuild a reply message from AgentEvent replay."""

    def __init__(self, message: Msg) -> None:
        self.message = message
        self._assembler = BlockAssembler()

    def append(self, event: AgentEvent) -> Msg:
        if event.reply_id != self.message.id:
            return self.message

        update = self._assembler.append(event)
        for block in update.started:
            self._append_block_once(block)
        for completion in update.completed:
            self._append_block_once(completion.block)

        match event.type:
            case EventType.REPLY_END:
                assert isinstance(event, ReplyEndEvent)
                self.message.finished_at = event.created_at

            case EventType.MODEL_CALL_END:
                assert isinstance(event, ModelCallEndEvent)
                if event.usage is not None:
                    if self.message.usage is None:
                        self.message.usage = Usage()
                    self.message.usage.input_tokens += event.usage.input_tokens
                    self.message.usage.output_tokens += event.usage.output_tokens
                    self.message.usage.total_tokens += event.usage.total_tokens

            case EventType.TOOL_RESULT_END:
                assert isinstance(event, ToolResultEndEvent)
                call = self._find_block("tool_call", event.tool_call_id)
                if isinstance(call, ToolCallBlock):
                    call.state = ToolCallState.FINISHED
                _remember_tool_observation_timing(
                    self.message,
                    tool_call_id=event.tool_call_id,
                    timing=event.observation_timing.to_message_projection_payload(),
                )

            case EventType.REQUIRE_USER_CONFIRM:
                assert isinstance(event, RequireUserConfirmEvent)
                for tool_call in event.tool_calls:
                    block = self._find_block("tool_call", tool_call.id)
                    if isinstance(block, ToolCallBlock):
                        block.state = ToolCallState.ASKING
                        block.suggested_rules = tool_call.suggested_rules

            case EventType.USER_CONFIRM_RESULT:
                assert isinstance(event, UserConfirmResultEvent)
                for result in event.confirm_results:
                    block = self._find_block("tool_call", result.tool_call.id)
                    if isinstance(block, ToolCallBlock):
                        block.state = (
                            ToolCallState.ALLOWED
                            if result.confirmed
                            else ToolCallState.FINISHED
                        )

            case EventType.REQUIRE_EXTERNAL_EXECUTION:
                assert isinstance(event, RequireExternalExecutionEvent)
                for requirement in event.external_tool_calls:
                    block = self._find_block("tool_call", requirement.tool_call_id)
                    if isinstance(block, ToolCallBlock):
                        block.state = ToolCallState.SUBMITTED

            case EventType.EXTERNAL_EXECUTION_RESULT:
                assert isinstance(event, ExternalExecutionResultEvent)
                for result in event.external_results:
                    tool_call_id = result.result_block.tool_call_id
                    block = self._find_block("tool_call", tool_call_id)
                    if isinstance(block, ToolCallBlock):
                        block.state = ToolCallState.FINISHED
                    _remember_tool_observation_timing(
                        self.message,
                        tool_call_id=tool_call_id,
                        timing=result.observation_timing.to_message_projection_payload(),
                    )

        return self.message

    def _find_block(self, block_type: str, block_id: str):
        for block in self.message.content:
            if (
                getattr(block, "type", None) == block_type
                and getattr(block, "id", None) == block_id
            ):
                return block
        return None

    def _append_block_once(self, block) -> None:
        if not any(existing is block for existing in self.message.content):
            self.message.content.append(block)


class MessageReplayControlError(RuntimeError):
    """A reply lacks the durable control authority required for replay."""


def accepted_main_reply_ids(events: tuple[AgentEvent, ...]) -> frozenset[str]:
    """Project model-visible main replies from durable lifecycle control facts."""

    main_starts: dict[str, ModelCallStartEvent] = {}
    model_ends: dict[str, list[ModelCallEndEvent]] = {}
    dispositions: dict[str, list[ModelCallControlDispositionResolvedEvent]] = {}
    reply_starts = {
        event.id: event for event in events if isinstance(event, ReplyStartEvent)
    }
    reply_ends = {
        event.id: event for event in events if isinstance(event, ReplyEndEvent)
    }
    for event in events:
        if (
            isinstance(event, ModelCallStartEvent)
            and event.recovery_plan.lifecycle_kind == "main_assistant_reply"
        ):
            call_id = event.resolved_call.resolved_model_call_id
            if call_id in main_starts:
                raise MessageReplayControlError(
                    "duplicate main model-call start identity"
                )
            main_starts[call_id] = event
        elif isinstance(event, ModelCallEndEvent):
            model_ends.setdefault(event.resolved_model_call_id, []).append(event)
        elif isinstance(event, ModelCallControlDispositionResolvedEvent):
            dispositions.setdefault(event.resolved_model_call_id, []).append(event)

    referenced_reply_event_ids: set[str] = set()
    accepted: set[str] = set()
    for call_id, start in main_starts.items():
        plan = start.recovery_plan
        reply_start_id = plan.reply_start_event_id
        reply_end_id = plan.stable_reply_end_event_id
        if reply_start_id is None or reply_end_id is None:
            raise MessageReplayControlError(
                "main model lifecycle lacks reply envelope identities"
            )
        reply_start = reply_starts.get(reply_start_id)
        reply_end = reply_ends.get(reply_end_id)
        if reply_start is None or reply_end is None:
            raise MessageReplayControlError(
                "main model lifecycle lacks its durable reply envelope"
            )
        referenced_reply_event_ids.update((reply_start_id, reply_end_id))
        if (
            reply_start.reply_id != start.reply_id
            or reply_end.reply_id != start.reply_id
            or reply_start.run_id != start.run_id
            or reply_end.run_id != start.run_id
        ):
            raise MessageReplayControlError(
                "main model lifecycle reply attribution mismatch"
            )

        ends = model_ends.get(call_id, [])
        if len(ends) != 1:
            raise MessageReplayControlError(
                "main model lifecycle requires exactly one terminal event"
            )
        end = ends[0]
        if (
            end.id != plan.stable_model_call_end_event_id
            or end.reply_id != start.reply_id
            or end.run_id != start.run_id
            or reply_end.model_terminal_outcome != end.outcome
        ):
            raise MessageReplayControlError(
                "main model lifecycle terminal attribution mismatch"
            )

        winners = dispositions.get(call_id, [])
        if end.outcome != "completed":
            if winners:
                raise MessageReplayControlError(
                    "non-completed model lifecycle cannot carry a disposition"
                )
            continue
        if len(winners) != 1:
            raise MessageReplayControlError(
                "completed model lifecycle requires exactly one disposition"
            )
        winner = winners[0]
        if (
            winner.model_call_start_event_id != start.id
            or winner.model_call_end_event_id != end.id
            or winner.model_call_index != start.model_call_index
            or winner.run_execution_activation
            != start.recovery_plan.run_execution_activation
        ):
            raise MessageReplayControlError(
                "model-call disposition does not join its lifecycle"
            )
        if winner.disposition is ModelCallControlDisposition.ACCEPTED:
            accepted.add(start.reply_id)

    orphan_reply_ids = (set(reply_starts) | set(reply_ends)) - referenced_reply_event_ids
    if orphan_reply_ids:
        raise MessageReplayControlError(
            "reply lifecycle is not owned by a main model-call start"
        )
    return frozenset(accepted)


def require_canonical_reply_control(events: tuple[AgentEvent, ...]) -> None:
    """Reject success replay unless every completed main call was accepted."""

    main_reply_ids = {
        event.reply_id
        for event in events
        if isinstance(event, ModelCallStartEvent)
        and event.recovery_plan.lifecycle_kind == "main_assistant_reply"
    }
    if not main_reply_ids:
        raise MessageReplayControlError(
            "canonical assistant replay requires a main model lifecycle"
        )
    if accepted_main_reply_ids(events) != frozenset(main_reply_ids):
        raise MessageReplayControlError(
            "completed model lifecycle lacks one accepted disposition"
        )


def _remember_tool_observation_timing(
    message: Msg, *, tool_call_id: str, timing: object
) -> None:
    if not isinstance(timing, dict):
        return
    by_call_id = message.metadata.setdefault("tool_observation_timing_by_call_id", {})
    if isinstance(by_call_id, dict):
        by_call_id[tool_call_id] = dict(timing)
    tool_result_blocks = [
        block
        for block in message.content
        if getattr(block, "type", None) == "tool_result"
    ]
    if (
        len(tool_result_blocks) == 1
        and getattr(tool_result_blocks[0], "id", None) == tool_call_id
    ):
        message.metadata["tool_observation_timing"] = dict(timing)


__all__ = [
    "MessageReducer",
    "MessageReplayControlError",
    "accepted_main_reply_ids",
    "require_canonical_reply_control",
]
