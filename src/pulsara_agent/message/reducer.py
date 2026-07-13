"""Replay AgentEvent streams into runtime messages."""

from __future__ import annotations

from pulsara_agent.event.events import (
    AgentEvent,
    EventType,
    ExternalExecutionResultEvent,
    ModelCallEndEvent,
    ReplyEndEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    ToolResultEndEvent,
    UserConfirmResultEvent,
)
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
