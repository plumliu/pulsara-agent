"""Fold AgentEvent streams into completed runtime content blocks."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.event.events import (
    AgentEvent,
    DataBlockSegmentEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    EventType,
    ExternalExecutionResultEvent,
    HintBlockEvent,
    TextBlockSegmentEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockSegmentEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallArgumentsSegmentEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.message.blocks import (
    Base64Source,
    ContentBlock,
    DataBlock,
    HintBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    URLSource,
)
from pulsara_agent.primitives.context import thaw_json


@dataclass(slots=True)
class BlockCompletion:
    block: ContentBlock
    reply_id: str
    block_id: str
    block_type: str
    start_sequence: int | None
    end_sequence: int | None
    start_event_id: str | None
    end_event_id: str


@dataclass(slots=True)
class BlockAssemblyUpdate:
    started: list[ContentBlock]
    completed: list[BlockCompletion]

    @classmethod
    def empty(cls) -> "BlockAssemblyUpdate":
        return cls(started=[], completed=[])


@dataclass(slots=True)
class _ActiveBlock:
    block: ContentBlock
    start_sequence: int | None
    start_event_id: str


class BlockAssembler:
    """Incrementally assemble completed content blocks from AgentEvents.

    Missing start events are treated as a recoverable stream problem: orphan
    deltas/ends are ignored here. Strict business entry points should validate
    their event slices before relying on the completed block.
    """

    def __init__(self) -> None:
        self._active: dict[tuple[str, str, str], _ActiveBlock] = {}

    def discard_reply(self, reply_id: str) -> int:
        keys = [key for key in self._active if key[0] == reply_id]
        for key in keys:
            self._active.pop(key, None)
        return len(keys)

    def active_count(self, reply_id: str | None = None) -> int:
        if reply_id is None:
            return len(self._active)
        return sum(1 for key in self._active if key[0] == reply_id)

    def append(self, event: AgentEvent) -> BlockAssemblyUpdate:
        match event.type:
            case EventType.TEXT_BLOCK_START:
                assert isinstance(event, TextBlockStartEvent)
                block = TextBlock(id=event.block_id, text="")
                self._start(event, "text", event.block_id, block)
                return BlockAssemblyUpdate(started=[block], completed=[])

            case EventType.TEXT_BLOCK_SEGMENT:
                assert isinstance(event, TextBlockSegmentEvent)
                active = self._get(event, "text", event.block_id)
                if active is not None and isinstance(active.block, TextBlock):
                    active.block.text += event.text

            case EventType.TEXT_BLOCK_END:
                assert isinstance(event, TextBlockEndEvent)
                return BlockAssemblyUpdate(
                    started=[], completed=self._complete(event, "text", event.block_id)
                )

            case EventType.THINKING_BLOCK_START:
                assert isinstance(event, ThinkingBlockStartEvent)
                block = ThinkingBlock(id=event.block_id, thinking="")
                self._start(
                    event,
                    "thinking",
                    event.block_id,
                    block,
                )
                return BlockAssemblyUpdate(started=[block], completed=[])

            case EventType.THINKING_BLOCK_SEGMENT:
                assert isinstance(event, ThinkingBlockSegmentEvent)
                active = self._get(event, "thinking", event.block_id)
                if active is not None and isinstance(active.block, ThinkingBlock):
                    active.block.thinking += event.thinking

            case EventType.THINKING_BLOCK_END:
                assert isinstance(event, ThinkingBlockEndEvent)
                return BlockAssemblyUpdate(
                    started=[],
                    completed=self._complete(event, "thinking", event.block_id),
                )

            case EventType.DATA_BLOCK_START:
                assert isinstance(event, DataBlockStartEvent)
                block = DataBlock(
                    id=event.block_id,
                    source=Base64Source(data="", media_type=event.media_type),
                )
                self._start(
                    event,
                    "data",
                    event.block_id,
                    block,
                )
                return BlockAssemblyUpdate(started=[block], completed=[])

            case EventType.DATA_BLOCK_SEGMENT:
                assert isinstance(event, DataBlockSegmentEvent)
                active = self._get(event, "data", event.block_id)
                if (
                    active is not None
                    and isinstance(active.block, DataBlock)
                    and isinstance(active.block.source, Base64Source)
                ):
                    active.block.source.data += event.data
                    active.block.source.media_type = (
                        event.media_type or active.block.source.media_type
                    )

            case EventType.DATA_BLOCK_END:
                assert isinstance(event, DataBlockEndEvent)
                return BlockAssemblyUpdate(
                    started=[], completed=self._complete(event, "data", event.block_id)
                )

            case EventType.HINT_BLOCK:
                assert isinstance(event, HintBlockEvent)
                block = HintBlock(
                    id=event.block_id, source=event.source, hint=event.hint
                )
                return BlockAssemblyUpdate(
                    started=[],
                    completed=[
                        BlockCompletion(
                            block=block,
                            reply_id=event.reply_id,
                            block_id=event.block_id,
                            block_type="hint",
                            start_sequence=event.sequence,
                            end_sequence=event.sequence,
                            start_event_id=event.id,
                            end_event_id=event.id,
                        )
                    ],
                )

            case EventType.TOOL_CALL_START:
                assert isinstance(event, ToolCallStartEvent)
                block = ToolCallBlock(
                    id=event.tool_call_id, name=event.tool_call_name, input=""
                )
                self._start(
                    event,
                    "tool_call",
                    event.tool_call_id,
                    block,
                )
                return BlockAssemblyUpdate(started=[block], completed=[])

            case EventType.TOOL_CALL_ARGUMENTS_SEGMENT:
                assert isinstance(event, ToolCallArgumentsSegmentEvent)
                active = self._get(event, "tool_call", event.tool_call_id)
                if active is not None and isinstance(active.block, ToolCallBlock):
                    active.block.input += event.arguments_json_fragment

            case EventType.TOOL_CALL_END:
                assert isinstance(event, ToolCallEndEvent)
                return BlockAssemblyUpdate(
                    started=[],
                    completed=self._complete(event, "tool_call", event.tool_call_id),
                )

            case EventType.TOOL_RESULT_START:
                assert isinstance(event, ToolResultStartEvent)
                block = ToolResultBlock(
                    id=event.tool_call_id,
                    name=event.tool_call_name,
                    output=[],
                    state=ToolResultState.RUNNING,
                )
                self._start(
                    event,
                    "tool_result",
                    event.tool_call_id,
                    block,
                )
                return BlockAssemblyUpdate(started=[block], completed=[])

            case EventType.TOOL_RESULT_TEXT_DELTA:
                assert isinstance(event, ToolResultTextDeltaEvent)
                active = self._get(event, "tool_result", event.tool_call_id)
                if active is not None and isinstance(active.block, ToolResultBlock):
                    if active.block.output and isinstance(
                        active.block.output[-1], TextBlock
                    ):
                        active.block.output[-1].text += event.delta
                    else:
                        active.block.output.append(TextBlock(text=event.delta))

            case EventType.TOOL_RESULT_DATA_DELTA:
                assert isinstance(event, ToolResultDataDeltaEvent)
                active = self._get(event, "tool_result", event.tool_call_id)
                if active is not None and isinstance(active.block, ToolResultBlock):
                    source = (
                        Base64Source(data=event.data, media_type=event.media_type)
                        if event.data is not None
                        else URLSource(url=str(event.url), media_type=event.media_type)
                    )
                    active.block.output.append(
                        DataBlock(id=event.block_id, source=source)
                    )

            case EventType.TOOL_RESULT_END:
                assert isinstance(event, ToolResultEndEvent)
                active = self._get(event, "tool_result", event.tool_call_id)
                if active is not None and isinstance(active.block, ToolResultBlock):
                    active.block.state = event.state
                    active.block.artifacts = list(event.artifacts)
                return BlockAssemblyUpdate(
                    started=[],
                    completed=self._complete(event, "tool_result", event.tool_call_id),
                )

            case EventType.EXTERNAL_EXECUTION_RESULT:
                assert isinstance(event, ExternalExecutionResultEvent)
                return BlockAssemblyUpdate(
                    started=[],
                    completed=[
                        BlockCompletion(
                            block=ToolResultBlock.model_validate(
                                thaw_json(result.result_block.canonical_block_payload)
                            ),
                            reply_id=event.reply_id,
                            block_id=result.result_block.tool_call_id,
                            block_type="tool_result",
                            start_sequence=event.sequence,
                            end_sequence=event.sequence,
                            start_event_id=event.id,
                            end_event_id=event.id,
                        )
                        for result in event.external_results
                    ],
                )

        return BlockAssemblyUpdate.empty()

    def _start(
        self, event: AgentEvent, block_type: str, block_id: str, block: ContentBlock
    ) -> None:
        self._active[(event.reply_id, block_type, block_id)] = _ActiveBlock(
            block=block,
            start_sequence=event.sequence,
            start_event_id=event.id,
        )

    def _get(
        self, event: AgentEvent, block_type: str, block_id: str
    ) -> _ActiveBlock | None:
        return self._active.get((event.reply_id, block_type, block_id))

    def _complete(
        self, event: AgentEvent, block_type: str, block_id: str
    ) -> list[BlockCompletion]:
        active = self._active.pop((event.reply_id, block_type, block_id), None)
        if active is None:
            return []
        return [
            BlockCompletion(
                block=active.block,
                reply_id=event.reply_id,
                block_id=block_id,
                block_type=block_type,
                start_sequence=active.start_sequence,
                end_sequence=event.sequence,
                start_event_id=active.start_event_id,
                end_event_id=event.id,
            )
        ]


def completed_tool_result_from_events(
    events: list[AgentEvent], tool_call_id: str
) -> ToolResultBlock:
    assembler = BlockAssembler()
    saw_matching_tool_result_event = False
    saw_start = False
    completed: ToolResultBlock | None = None

    for event in events:
        if getattr(event, "tool_call_id", None) != tool_call_id:
            continue
        if not isinstance(
            event,
            (
                ToolResultStartEvent,
                ToolResultTextDeltaEvent,
                ToolResultDataDeltaEvent,
                ToolResultEndEvent,
            ),
        ):
            continue

        saw_matching_tool_result_event = True
        if not saw_start and not isinstance(event, ToolResultStartEvent):
            if isinstance(event, ToolResultEndEvent):
                raise ValueError(
                    f"Tool result end without start for tool_call_id: {tool_call_id}"
                )
            raise ValueError(
                f"Tool result delta without start for tool_call_id: {tool_call_id}"
            )
        if isinstance(event, ToolResultStartEvent):
            saw_start = True

        for completion in assembler.append(event).completed:
            if (
                isinstance(completion.block, ToolResultBlock)
                and completion.block.id == tool_call_id
            ):
                completed = completion.block

    if completed is not None:
        return completed
    if not saw_matching_tool_result_event:
        raise KeyError(
            f"No tool result found in event slice for tool_call_id: {tool_call_id}"
        )
    if saw_start:
        raise ValueError(
            f"Tool result slice missing end for tool_call_id: {tool_call_id}"
        )
    raise ValueError(f"Malformed tool result slice for tool_call_id: {tool_call_id}")
