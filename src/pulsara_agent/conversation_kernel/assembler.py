"""Single process-local assembler for the formal provider live stream."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live import LiveBlockKind
from pulsara_agent.conversation_kernel.live import LiveChannelKind
from pulsara_agent.ports.live_agent_event import (
    DataDeltaPayload,
    DataEndPayload,
    DataStartPayload,
    LivePayload,
    ProviderStreamPayload,
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ThinkingDeltaPayload,
    ThinkingEndPayload,
    ThinkingStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.primitives.context import FrozenJsonObjectFact, freeze_json


@dataclass(frozen=True, slots=True)
class CompletedTextBlock:
    block_id: str
    text: str


@dataclass(frozen=True, slots=True)
class CompletedDataBlock:
    block_id: str
    media_type: str
    data: str


@dataclass(frozen=True, slots=True)
class CompletedToolCallBlock:
    block_id: str
    tool_call_id: str
    tool_name: str
    arguments: FrozenJsonObjectFact

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, FrozenJsonObjectFact):
            raise TypeError("completed tool-call arguments must be recursively frozen")


CompletedBlock = CompletedTextBlock | CompletedDataBlock | CompletedToolCallBlock


@dataclass(frozen=True, slots=True)
class CompletedAssistantMessage:
    draft_identity: str
    blocks: tuple[CompletedBlock, ...]
    public_text: str


@dataclass(slots=True)
class _OpenBlock:
    kind: str
    name_or_media: str | None
    canonical_block_id: str
    ordinal: int
    chunks: list[str]


class ProviderStreamAssembler:
    def __init__(
        self,
        *,
        session_id: str,
        turn_id: str,
        live_bus: LiveAgentEventBus,
        proposed_entry_id: str,
        conversation_scope_kind: str = "ROOT",
        scope_subagent_task_id: str | None = None,
        maximum_completed_bytes: int = 4 << 20,
    ) -> None:
        if maximum_completed_bytes < 1:
            raise ValueError("assembler bound must be positive")
        if not proposed_entry_id:
            raise ValueError("proposed assistant entry identity is required")
        self._session_id = session_id
        self._turn_id = turn_id
        self._live_bus = live_bus
        self._maximum_completed_bytes = maximum_completed_bytes
        # Live draft and accepted canonical entry deliberately share one
        # identity.  The live plane remains disposable; the later canonical
        # commit unconditionally retires the matching draft in every reader.
        self._draft_identity = proposed_entry_id
        self._scope_kind = conversation_scope_kind
        self._scope_subagent_task_id = scope_subagent_task_id
        if (conversation_scope_kind == "ROOT") != (scope_subagent_task_id is None):
            raise ValueError("provider live scope union is invalid")
        self._open: dict[str, _OpenBlock] = {}
        self._seen: set[str] = set()
        # Completion order is not semantic order. Providers may interleave
        # blocks and finish a later block first, so the Start-time ordinal is
        # the sole ordering authority for the eventual canonical message.
        self._completed: dict[int, CompletedBlock] = {}
        self._completed_bytes = 0

    @property
    def draft_identity(self) -> str:
        return self._draft_identity

    def apply(self, item: ProviderStreamPayload) -> None:
        if isinstance(item, TextStartPayload):
            self._start(item.block_identity, "text", None, LiveEventType.TEXT_START)
        elif isinstance(item, ThinkingStartPayload):
            self._start(
                item.block_identity, "thinking", None, LiveEventType.THINKING_START
            )
        elif isinstance(item, DataStartPayload):
            self._start(
                item.block_identity,
                "data",
                item.media_type,
                LiveEventType.DATA_START,
            )
        elif isinstance(item, ToolCallStartPayload):
            if item.block_identity != item.tool_call_id:
                raise RuntimeError("provider tool-call start identity mismatch")
            self._start(
                item.tool_call_id,
                "tool",
                item.tool_name,
                LiveEventType.TOOL_CALL_START,
            )
        elif isinstance(item, TextDeltaPayload):
            self._delta(
                item.block_identity, "text", item.delta, LiveEventType.TEXT_DELTA
            )
        elif isinstance(item, ThinkingDeltaPayload):
            self._delta(
                item.block_identity,
                "thinking",
                item.delta,
                LiveEventType.THINKING_DELTA,
            )
        elif isinstance(item, DataDeltaPayload):
            self._delta(
                item.block_identity, "data", item.data, LiveEventType.DATA_DELTA
            )
        elif isinstance(item, ToolCallDeltaPayload):
            if item.block_identity != item.tool_call_id:
                raise RuntimeError("provider tool-call delta identity mismatch")
            self._delta(
                item.tool_call_id,
                "tool",
                item.delta,
                LiveEventType.TOOL_CALL_DELTA,
            )
        elif isinstance(item, TextEndPayload):
            self._end(
                item.block_identity,
                "text",
                LiveEventType.TEXT_END,
                expected_final=item.final_text,
            )
        elif isinstance(item, ThinkingEndPayload):
            self._end(
                item.block_identity,
                "thinking",
                LiveEventType.THINKING_END,
                expected_final=item.final_text,
            )
        elif isinstance(item, DataEndPayload):
            self._end(
                item.block_identity,
                "data",
                LiveEventType.DATA_END,
                expected_final=item.final_data,
                expected_name=item.media_type,
            )
        elif isinstance(item, ToolCallEndPayload):
            if item.block_identity != item.tool_call_id:
                raise RuntimeError("provider tool-call end identity mismatch")
            self._end(
                item.tool_call_id,
                "tool",
                LiveEventType.TOOL_CALL_END,
                expected_final=item.arguments_json,
                expected_name=item.tool_name,
            )
        else:  # pragma: no cover - closed provider stream union
            raise TypeError(type(item).__name__)

    def complete(self) -> CompletedAssistantMessage:
        if self._open:
            raise RuntimeError("provider stream ended with open blocks")
        ordered = tuple(self._completed[index] for index in sorted(self._completed))
        text = "".join(
            block.text for block in ordered if isinstance(block, CompletedTextBlock)
        )
        return CompletedAssistantMessage(
            draft_identity=self._draft_identity,
            blocks=ordered,
            public_text=text,
        )

    def _start(
        self,
        identity: str,
        kind: str,
        name_or_media: str | None,
        event_type: LiveEventType,
    ) -> None:
        if identity in self._seen:
            raise RuntimeError("provider block identity was reused")
        self._seen.add(identity)
        canonical_id = _canonical_block_id(self._draft_identity, kind, identity)
        block = _OpenBlock(
            kind,
            name_or_media,
            canonical_id,
            len(self._seen) - 1,
            [],
        )
        self._open[identity] = block
        payload: LivePayload
        if kind == "text":
            payload = TextStartPayload(block.canonical_block_id)
        elif kind == "thinking":
            payload = ThinkingStartPayload(block.canonical_block_id)
        elif kind == "data":
            assert name_or_media is not None
            payload = DataStartPayload(block.canonical_block_id, name_or_media)
        else:
            assert name_or_media is not None
            payload = ToolCallStartPayload(
                block.canonical_block_id, identity, name_or_media
            )
        self._offer(event_type, block, payload)

    def _delta(
        self,
        identity: str,
        kind: str,
        value: str,
        event_type: LiveEventType,
    ) -> None:
        block = self._open.get(identity)
        if block is None or block.kind != kind:
            raise RuntimeError("provider delta lacks an exact open block")
        size = len(value.encode("utf-8"))
        if self._completed_bytes + size > self._maximum_completed_bytes:
            raise RuntimeError("provider assistant draft exceeds bounded assembly")
        self._completed_bytes += size
        block.chunks.append(value)
        if kind == "text":
            payload: LivePayload = TextDeltaPayload(block.canonical_block_id, value)
        elif kind == "thinking":
            payload = ThinkingDeltaPayload(block.canonical_block_id, value)
        elif kind == "data":
            payload = DataDeltaPayload(block.canonical_block_id, value)
        else:
            payload = ToolCallDeltaPayload(block.canonical_block_id, identity, value)
        self._offer(event_type, block, payload)

    def _end(
        self,
        identity: str,
        kind: str,
        event_type: LiveEventType,
        *,
        expected_final: str,
        expected_name: str | None = None,
    ) -> None:
        block = self._open.pop(identity, None)
        if block is None or block.kind != kind:
            raise RuntimeError("provider end lacks an exact open block")
        value = "".join(block.chunks)
        if value != expected_final:
            raise RuntimeError("provider terminal block differs from its deltas")
        if expected_name is not None and block.name_or_media != expected_name:
            raise RuntimeError("provider terminal block metadata changed")
        completed: CompletedBlock | None
        if kind == "text":
            completed = CompletedTextBlock(block.canonical_block_id, value)
        elif kind == "data":
            assert block.name_or_media is not None
            completed = CompletedDataBlock(
                block.canonical_block_id, block.name_or_media, value
            )
        elif kind == "tool":
            assert block.name_or_media is not None
            try:
                parsed = json.loads(value or "{}")
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "provider tool arguments are not valid JSON"
                ) from exc
            if not isinstance(parsed, dict):
                raise RuntimeError("provider tool arguments must be an object")
            frozen_arguments = freeze_json(parsed)
            if not isinstance(frozen_arguments, FrozenJsonObjectFact):
                raise RuntimeError("provider tool arguments must freeze as an object")
            completed = CompletedToolCallBlock(
                block_id=block.canonical_block_id,
                tool_call_id=identity,
                tool_name=block.name_or_media,
                arguments=frozen_arguments,
            )
        else:
            # Thinking remains process-local, but its Start ordinal still
            # orders surrounding canonical blocks.
            completed = None
        if completed is not None:
            if block.ordinal in self._completed:
                raise RuntimeError("provider block ordinal was completed twice")
            self._completed[block.ordinal] = completed
        # Thinking is intentionally process-local and absent from completed
        # canonical blocks, but its terminal live view remains exact.
        if kind == "text":
            payload: LivePayload = TextEndPayload(
                block.canonical_block_id,
                value,
                len(value.encode("utf-8")),
                live_digest(value),
            )
        elif kind == "thinking":
            payload = ThinkingEndPayload(
                block.canonical_block_id,
                value,
                len(value.encode("utf-8")),
                live_digest(value),
            )
        elif kind == "data":
            assert block.name_or_media is not None
            payload = DataEndPayload(
                block.canonical_block_id,
                block.name_or_media,
                value,
                len(value.encode("utf-8")),
                live_digest(value),
            )
        else:
            assert block.name_or_media is not None
            arguments_json = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            payload = ToolCallEndPayload(
                block.canonical_block_id,
                identity,
                block.name_or_media,
                arguments_json,
                len(arguments_json.encode("utf-8")),
                live_digest(arguments_json),
            )
        self._offer(event_type, block, payload)

    def _offer(
        self,
        event_type: LiveEventType,
        block: _OpenBlock,
        payload: LivePayload,
    ) -> None:
        self._live_bus.offer_nowait(
            event_type=event_type,
            session_id=self._session_id,
            turn_id=self._turn_id,
            draft_identity=self._draft_identity,
            payload=payload,
            scope_kind=self._scope_kind,
            scope_subagent_task_id=self._scope_subagent_task_id,
            channel_kind=LiveChannelKind.MODEL_OUTPUT,
            generation_id=f"model-output:{self._draft_identity}",
            proposed_entry_id=self._draft_identity,
            block_id=block.canonical_block_id,
            block_ordinal=block.ordinal,
            block_kind={
                "text": LiveBlockKind.TEXT,
                "thinking": LiveBlockKind.THINKING,
                "data": LiveBlockKind.DATA,
                "tool": LiveBlockKind.TOOL_CALL,
            }[block.kind],
        )


def _canonical_block_id(entry_id: str, kind: str, provider_identity: str) -> str:
    digest = sha256(f"{entry_id}\0{kind}\0{provider_identity}".encode()).hexdigest()
    return f"block:{digest}"


__all__ = [
    "CompletedAssistantMessage",
    "CompletedBlock",
    "CompletedDataBlock",
    "CompletedTextBlock",
    "CompletedToolCallBlock",
    "ProviderStreamAssembler",
]
