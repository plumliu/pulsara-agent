"""Direct foreground model-port regression tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.provider_stream import (
    ProviderPhysicalCompletion,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from tests.support.model_config import test_llm_config


class _TerminalExecution:
    def __init__(self) -> None:
        self._delivered = False
        self.closed = False

    async def read_next(self):
        if self._delivered:
            return None
        self._delivered = True
        return ProviderStreamTerminal(
            outcome="COMPLETED",
            usage=TransportUsageReport(usage_status="missing", usage=None),
        )

    async def aclose(self) -> None:
        self.closed = True

    async def wait_physical_completion(self) -> ProviderPhysicalCompletion:
        return ProviderPhysicalCompletion(
            status=ProviderPhysicalCompletionStatus.COMPLETED,
            diagnostic_code=None,
        )


class _TerminalTransport:
    def __init__(self) -> None:
        self.execution = _TerminalExecution()
        self.context = None

    def open_stream(self, *, call, context):
        del call
        self.context = context
        return self.execution


def test_stage2_direct_model_uses_current_resolved_output_budget_field(
    monkeypatch,
) -> None:
    import pulsara_agent.conversation_kernel.direct_model as direct_model

    transport = _TerminalTransport()
    target = SimpleNamespace(
        context_budget=SimpleNamespace(effective_output_tokens=64),
        fact=SimpleNamespace(target_fingerprint="sha256:" + "2" * 64),
        transport=transport,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )
    monkeypatch.setattr(direct_model, "resolve_model_target", lambda **_: target)
    monkeypatch.setattr(
        direct_model,
        "resolve_model_call",
        lambda **_: SimpleNamespace(
            resolved_model_call_id="model-call:test", target=target
        ),
    )
    monkeypatch.setattr(
        direct_model,
        "validate_model_context_for_call",
        lambda *, call, context: SimpleNamespace(
            estimate=call.target.token_estimator.estimate_context(context)
        ),
    )
    port = DirectKernelModelPort(
        config=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        ),
        tools=(
            ToolSpec(
                name="read_file",
                description="Read one file from the active workspace.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            ),
        ),
        system_prompt="ROOT SYSTEM",
    )
    request = SimpleNamespace(
        provider_input=SimpleNamespace(canonical_bytes=4, items=()),
        model_call_index=1,
        system_prompt=None,
        maximum_input_tokens=4_096,
        maximum_output_tokens=64,
    )

    async def collect() -> list[object]:
        return [item async for item in port.stream(request)]  # type: ignore[arg-type]

    assert asyncio.run(collect()) == []
    assert transport.context.compiler_estimated_input_tokens > 1
    assert transport.context.system_prompt == "ROOT SYSTEM"
    assert transport.context.tools[0].name == "read_file"
    assert transport.execution.closed is True


def test_stage2_direct_model_rejects_resolved_input_before_physical_send(
    monkeypatch,
) -> None:
    import pulsara_agent.conversation_kernel.direct_model as direct_model

    transport = _TerminalTransport()
    target = SimpleNamespace(
        context_budget=SimpleNamespace(effective_output_tokens=64),
        fact=SimpleNamespace(target_fingerprint="sha256:" + "2" * 64),
        transport=transport,
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )
    monkeypatch.setattr(direct_model, "resolve_model_target", lambda **_: target)
    monkeypatch.setattr(
        direct_model,
        "resolve_model_call",
        lambda **_: SimpleNamespace(
            resolved_model_call_id="model-call:test", target=target
        ),
    )
    port = DirectKernelModelPort(
        config=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        ),
        tools=(ToolSpec("tool", "large schema", {"type": "object"}),),
        system_prompt="ROOT SYSTEM",
    )
    request = SimpleNamespace(
        provider_input=SimpleNamespace(canonical_bytes=1, items=()),
        model_call_index=1,
        system_prompt=None,
        maximum_input_tokens=1,
        maximum_output_tokens=64,
    )

    async def collect() -> list[object]:
        return [item async for item in port.stream(request)]  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="provider input"):
        asyncio.run(collect())
    assert transport.context is None


def test_stage2_direct_model_real_adapter_path_emits_only_live_payloads() -> None:
    port = DirectKernelModelPort(
        config=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        ),
    )
    binding = port._registry.get("openai_chat_completions")
    binding._adapter._mock_chunks = [
        {"choices": [{"delta": {"content": "hello"}}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        },
    ]
    request = SimpleNamespace(
        provider_input=SimpleNamespace(canonical_bytes=1, items=()),
        model_call_index=1,
        system_prompt=None,
        maximum_input_tokens=4_096,
        maximum_output_tokens=16_384,
    )

    async def collect() -> list[object]:
        return [item async for item in port.stream(request)]  # type: ignore[arg-type]

    values = asyncio.run(collect())
    assert [type(value) for value in values] == [
        TextStartPayload,
        TextDeltaPayload,
        TextEndPayload,
    ]
    assert not any("Draft" in type(value).__name__ for value in values)
