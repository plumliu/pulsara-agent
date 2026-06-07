"""Replay AgentEvent streams into runtime messages."""

from __future__ import annotations

from pulsara_agent.event.events import (
    AgentEvent,
    DataBlockDeltaEvent,
    DataBlockStartEvent,
    EventType,
    ExternalExecutionResultEvent,
    HintBlockEvent,
    ModelCallEndEvent,
    ReplyEndEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.message.blocks import (
    Base64Source,
    DataBlock,
    HintBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    URLSource,
)
from pulsara_agent.message.message import Msg, Usage


class MessageReducer:
    """Rebuild a reply message from AgentEvent replay."""

    def __init__(self, message: Msg) -> None:
        self.message = message

    def append(self, event: AgentEvent) -> Msg:
        if event.reply_id != self.message.id:
            return self.message

        match event.type:
            case EventType.REPLY_END:
                assert isinstance(event, ReplyEndEvent)
                self.message.finished_at = event.created_at

            case EventType.MODEL_CALL_END:
                assert isinstance(event, ModelCallEndEvent)
                if self.message.usage is None:
                    self.message.usage = Usage()
                self.message.usage.input_tokens += event.input_tokens
                self.message.usage.output_tokens += event.output_tokens
                self.message.usage.total_tokens += event.total_tokens

            case EventType.TEXT_BLOCK_START:
                assert isinstance(event, TextBlockStartEvent)
                self.message.content.append(TextBlock(id=event.block_id, text=""))

            case EventType.TEXT_BLOCK_DELTA:
                assert isinstance(event, TextBlockDeltaEvent)
                block = self._find_block("text", event.block_id)
                if isinstance(block, TextBlock):
                    block.text += event.delta

            case EventType.DATA_BLOCK_START:
                assert isinstance(event, DataBlockStartEvent)
                self.message.content.append(
                    DataBlock(
                        id=event.block_id,
                        source=Base64Source(data="", media_type=event.media_type),
                    )
                )

            case EventType.DATA_BLOCK_DELTA:
                assert isinstance(event, DataBlockDeltaEvent)
                block = self._find_block("data", event.block_id)
                if isinstance(block, DataBlock) and isinstance(block.source, Base64Source):
                    block.source.data += event.data

            case EventType.THINKING_BLOCK_START:
                assert isinstance(event, ThinkingBlockStartEvent)
                self.message.content.append(ThinkingBlock(id=event.block_id, thinking=""))

            case EventType.THINKING_BLOCK_DELTA:
                assert isinstance(event, ThinkingBlockDeltaEvent)
                block = self._find_block("thinking", event.block_id)
                if isinstance(block, ThinkingBlock):
                    block.thinking += event.delta

            case EventType.HINT_BLOCK:
                assert isinstance(event, HintBlockEvent)
                self.message.content.append(
                    HintBlock(id=event.block_id, source=event.source, hint=event.hint)
                )

            case EventType.TOOL_CALL_START:
                assert isinstance(event, ToolCallStartEvent)
                self.message.content.append(
                    ToolCallBlock(
                        id=event.tool_call_id,
                        name=event.tool_call_name,
                        input="",
                    )
                )

            case EventType.TOOL_CALL_DELTA:
                assert isinstance(event, ToolCallDeltaEvent)
                block = self._find_block("tool_call", event.tool_call_id)
                if isinstance(block, ToolCallBlock):
                    block.input += event.delta

            case EventType.TOOL_RESULT_START:
                assert isinstance(event, ToolResultStartEvent)
                self.message.content.append(
                    ToolResultBlock(
                        id=event.tool_call_id,
                        name=event.tool_call_name,
                        output=[],
                        state=ToolResultState.RUNNING,
                    )
                )

            case EventType.TOOL_RESULT_TEXT_DELTA:
                assert isinstance(event, ToolResultTextDeltaEvent)
                block = self._find_block("tool_result", event.tool_call_id)
                if isinstance(block, ToolResultBlock):
                    if not block.output or block.output[-1].type != "text":
                        block.output.append(TextBlock(text=event.delta))
                    else:
                        text_block = block.output[-1]
                        assert isinstance(text_block, TextBlock)
                        text_block.text += event.delta

            case EventType.TOOL_RESULT_DATA_DELTA:
                assert isinstance(event, ToolResultDataDeltaEvent)
                block = self._find_block("tool_result", event.tool_call_id)
                if isinstance(block, ToolResultBlock):
                    source = (
                        Base64Source(data=event.data, media_type=event.media_type)
                        if event.data is not None
                        else URLSource(url=str(event.url), media_type=event.media_type)
                    )
                    block.output.append(DataBlock(id=event.block_id, source=source))

            case EventType.TOOL_RESULT_END:
                assert isinstance(event, ToolResultEndEvent)
                block = self._find_block("tool_result", event.tool_call_id)
                if isinstance(block, ToolResultBlock):
                    block.state = event.state
                call = self._find_block("tool_call", event.tool_call_id)
                if isinstance(call, ToolCallBlock):
                    call.state = ToolCallState.FINISHED

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
                        block.state = ToolCallState.ALLOWED if result.confirmed else ToolCallState.FINISHED

            case EventType.REQUIRE_EXTERNAL_EXECUTION:
                assert isinstance(event, RequireExternalExecutionEvent)
                for tool_call in event.tool_calls:
                    block = self._find_block("tool_call", tool_call.id)
                    if isinstance(block, ToolCallBlock):
                        block.state = ToolCallState.SUBMITTED

            case EventType.EXTERNAL_EXECUTION_RESULT:
                assert isinstance(event, ExternalExecutionResultEvent)
                self.message.content.extend(result.model_copy(deep=True) for result in event.execution_results)

        return self.message

    def _find_block(self, block_type: str, block_id: str):
        for block in self.message.content:
            if getattr(block, "type", None) == block_type and getattr(block, "id", None) == block_id:
                return block
        return None
