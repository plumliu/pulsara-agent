"""Canonical live text and tool-call events for controlled model tests."""

import json

from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload, TextEndPayload, TextStartPayload,
    ToolCallDeltaPayload, ToolCallEndPayload, ToolCallStartPayload, live_digest,
)


def _tool_call(
    name: str, call_id: str, arguments: dict[str, object]
) -> tuple[object, ...]:
    encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return (
        ToolCallStartPayload(call_id, call_id, name),
        ToolCallDeltaPayload(call_id, call_id, encoded),
        ToolCallEndPayload(
            block_identity=call_id,
            tool_call_id=call_id,
            tool_name=name,
            arguments_json=encoded,
            utf8_bytes=len(encoded.encode("utf-8")),
            digest=live_digest(encoded),
        ),
    )


def _text(block_id: str, value: str) -> tuple[object, ...]:
    return (
        TextStartPayload(block_id),
        TextDeltaPayload(block_id, value),
        TextEndPayload(
            block_id,
            value,
            len(value.encode("utf-8")),
            live_digest(value),
        ),
    )
