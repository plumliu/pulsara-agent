"""Single-vocabulary provider stream regression tests for Stage 2."""

from __future__ import annotations

import asyncio

from pulsara_agent.llm.adapters.openai.events import ProviderLiveItemBuilder
from pulsara_agent.llm.normalized_transport import (
    NormalizedProviderTransportExecution,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.ports.provider_stream import (
    ProviderPhysicalCompletionStatus,
    ProviderStreamFailure,
    ProviderStreamTerminal,
)


async def _stream(items):
    for item in items:
        yield item


def test_stage2_provider_stream_uses_formal_live_payloads_without_adoption() -> None:
    text = "hello"
    execution = NormalizedProviderTransportExecution(
        _stream(
            (
                TextStartPayload("provider:text"),
                TextDeltaPayload("provider:text", text),
                TextEndPayload(
                    "provider:text",
                    text,
                    len(text.encode("utf-8")),
                    live_digest(text),
                ),
                TransportUsageReport(usage_status="missing", usage=None),
            )
        )
    )

    async def collect():
        values = []
        while True:
            value = await execution.read_next()
            if value is None:
                break
            values.append(value)
        await execution.aclose()
        return values, await execution.wait_physical_completion()

    values, completion = asyncio.run(collect())
    assert [type(value) for value in values] == [
        TextStartPayload,
        TextDeltaPayload,
        TextEndPayload,
        ProviderStreamTerminal,
    ]
    assert values[-1].outcome == "COMPLETED"
    assert not hasattr(execution, "require_adoptable")
    assert not hasattr(execution, "acknowledge_adopted")
    assert completion.status is ProviderPhysicalCompletionStatus.COMPLETED


def test_stage2_provider_stream_terminal_view_must_match_delta_prefix() -> None:
    execution = NormalizedProviderTransportExecution(
        _stream(
            (
                TextStartPayload("provider:text"),
                TextDeltaPayload("provider:text", "prefix"),
                TextEndPayload(
                    "provider:text",
                    "different",
                    len("different"),
                    live_digest("different"),
                ),
            )
        )
    )

    async def collect():
        first = await execution.read_next()
        second = await execution.read_next()
        terminal = await execution.read_next()
        await execution.aclose()
        return first, second, terminal

    first, second, terminal = asyncio.run(collect())
    assert isinstance(first, TextStartPayload)
    assert isinstance(second, TextDeltaPayload)
    assert isinstance(terminal, ProviderStreamTerminal)
    assert terminal.outcome == "PROVIDER_ERROR"
    assert terminal.error is not None


def test_stage2_provider_failure_is_sanitized_at_the_single_boundary() -> None:
    execution = NormalizedProviderTransportExecution(
        _stream(
            (
                ProviderStreamFailure(
                    message=(
                        "Authorization: Bearer top-secret "
                        "https://user:pass@example.test/path?q=secret"
                    ),
                    code_hint="401_auth",
                ),
            )
        )
    )

    terminal = asyncio.run(execution.read_next())
    assert isinstance(terminal, ProviderStreamTerminal)
    assert terminal.error is not None
    assert "top-secret" not in terminal.error.message
    assert "user:pass" not in terminal.error.message
    assert "q=secret" not in terminal.error.message


def test_openai_builder_constructs_exact_live_payloads_and_frozen_end() -> None:
    builder = ProviderLiveItemBuilder()
    stream = []
    stream.extend(
        builder.tool_call_start(
            tool_call_id="call:1",
            tool_call_name="read_file",
        )
    )
    stream.extend(builder.tool_call_delta(tool_call_id="call:1", delta='{"p":"x"}'))
    stream.extend(builder.tool_call_end(tool_call_id="call:1"))

    assert [type(item) for item in stream] == [
        ToolCallStartPayload,
        ToolCallDeltaPayload,
        ToolCallEndPayload,
    ]
    terminal = stream[-1]
    assert isinstance(terminal, ToolCallEndPayload)
    assert terminal.arguments_json == '{"p":"x"}'
    assert terminal.digest == live_digest(terminal.arguments_json)
