"""Direct foreground model-port regression tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort,
    KernelModelExecutionRequest,
    KernelModelPreparationRequest,
)
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    ModelInputScopeKind,
    PreparedProviderInputCut,
    StructuredModelInputCompileRequest,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
)
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.support.model_config import test_llm_config
from tests.support.round3 import StaticContextSourceCollector, StructuredToolPort


def _prepared_execution(
    port: DirectKernelModelPort,
    *,
    maximum_input_tokens: int = 4_096,
    scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
) -> tuple[KernelModelExecutionRequest, StructuredToolPort]:
    session_id = "session:test"
    turn_id = "turn:test"
    revision_id = "binding:test"
    sequence = 0
    tool_port = StructuredToolPort(object(), tool_names=("read_file",))
    surface = tool_port.snapshot_tool_surface(
        conversation_scope_kind=scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    prepared = port.prepare_call(
        KernelModelPreparationRequest(
            session_id=session_id,
            turn_id=turn_id,
            model_call_index=1,
            purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
            maximum_input_tokens=maximum_input_tokens,
            maximum_output_tokens=16_384,
            tool_surface=surface,
        )
    )
    identity = CanonicalModelInputIdentity(
        session_id=session_id,
        turn_id=turn_id,
        context_binding_revision_id=revision_id,
        provider_input_through_sequence=sequence,
        conversation_scope_kind=scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            session_id=session_id,
            turn_id=turn_id,
            context_binding_revision_id=revision_id,
            provider_input_through_sequence=sequence,
            conversation_scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
        ),
    )
    snapshot = CanonicalModelInputSnapshot(
        identity=identity,
        items=(),
        canonical_utf8_bytes=0,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=(),
            canonical_utf8_bytes=0,
            closures=(),
            late_outcomes=(),
        ),
    )
    sources = StaticContextSourceCollector().collect()
    compiled = StructuredModelInputCompiler().compile(
        StructuredModelInputCompileRequest(
            context_id="context:test",
            model_call_index=1,
            canonical_input=snapshot,
            compile_binding=prepared.compile_binding,
            sources=sources,
        )
    )
    borrow = tool_port.borrow_tool_surface(surface)
    return (
        KernelModelExecutionRequest(
            session_id=session_id,
            turn_id=turn_id,
            model_call_index=1,
            prepared_call=prepared,
            compiled_input=compiled,
            cut=PreparedProviderInputCut(
                session_id=session_id,
                turn_id=turn_id,
                context_binding_revision_id=revision_id,
                provider_input_through_sequence=sequence,
            ),
            surface_borrow=borrow,
        ),
        tool_port,
    )


def _port(*, usage_observer=None) -> DirectKernelModelPort:
    return DirectKernelModelPort(
        config=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        ),
        usage_observer=usage_observer,
    )


def test_stage2_direct_model_freezes_output_budget_system_and_tools() -> None:
    port = _port()
    request, _tool_port = _prepared_execution(port)
    prepared = request.prepared_call
    compiled = request.compiled_input

    assert prepared.compile_binding.effective_output_tokens <= 16_384
    assert compiled.system_prompt.startswith("ROOT SYSTEM")
    assert [item.name for item in compiled.tools] == ["read_file"]
    assert (
        compiled.final_estimate
        == prepared.compile_binding.estimator.estimate_frozen_input(
            system_prompt=compiled.system_prompt,
            messages=compiled.messages,
            tools=compiled.tools,
        )
    )
    request.surface_borrow.close()


def test_stage2_direct_model_rejects_invalid_compiled_input_before_send() -> None:
    port = _port()
    request, _tool_port = _prepared_execution(port)
    other, _other_tool_port = _prepared_execution(port)
    invalid = replace(request, compiled_input=other.compiled_input)

    async def collect() -> list[object]:
        return [item async for item in port.stream(invalid)]

    with pytest.raises(ValueError, match="exact-join preparation"):
        asyncio.run(collect())
    request.surface_borrow.close()
    other.surface_borrow.close()


def test_round3_execution_exactly_joins_provider_cut_before_open() -> None:
    port = _port()
    request, _tool_port = _prepared_execution(port)
    with pytest.raises(ValueError, match="structurally joined"):
        replace(
            request,
            cut=replace(
                request.cut,
                context_binding_revision_id="binding:other",
                provider_input_through_sequence=1,
            ),
        )
    request.surface_borrow.close()


def test_round3_execution_rejects_other_subagent_surface_access() -> None:
    port = _port()
    request_a, _tool_port_a = _prepared_execution(
        port,
        scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
        scope_subagent_task_id="subagent-task:a",
    )
    request_b, _tool_port_b = _prepared_execution(
        port,
        scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
        scope_subagent_task_id="subagent-task:b",
    )
    with pytest.raises(ValueError, match="structurally joined"):
        replace(
            request_b,
            compiled_input=request_a.compiled_input,
            cut=request_a.cut,
        )
    request_a.surface_borrow.close()
    request_b.surface_borrow.close()


def test_round3_execution_rejects_same_shape_foreign_host_surface_borrow() -> None:
    port = _port()
    request_a, _tool_port_a = _prepared_execution(port)
    request_b, _tool_port_b = _prepared_execution(port)

    assert request_a.prepared_call.tool_surface != request_b.prepared_call.tool_surface
    with pytest.raises(ValueError, match="structurally joined"):
        replace(request_a, surface_borrow=request_b.surface_borrow)

    request_a.surface_borrow.close()
    request_b.surface_borrow.close()


def test_round3_direct_model_rejects_final_estimate_drift_before_transport_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port()
    request, _tool_port = _prepared_execution(port)
    compiled = request.compiled_input
    divergent = replace(
        compiled.final_estimate,
        envelope_tokens=compiled.final_estimate.envelope_tokens + 1,
        total_input_tokens=compiled.final_estimate.total_input_tokens + 1,
    )
    opened = 0

    def validate_differently(*, call, context):
        del call, context
        return SimpleNamespace(estimate=divergent)

    def forbidden_open(*, call, context):
        del call, context
        nonlocal opened
        opened += 1
        raise AssertionError("transport must not open after estimate drift")

    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.direct_model.validate_model_context_for_call",
        validate_differently,
    )
    monkeypatch.setattr(
        request.prepared_call.call.target.transport, "open_stream", forbidden_open
    )

    async def collect() -> list[object]:
        return [item async for item in port.stream(request)]

    with pytest.raises(RuntimeError, match="pre-send input estimates differ"):
        asyncio.run(collect())
    assert opened == 0
    request.surface_borrow.close()


def test_stage2_direct_model_real_adapter_path_emits_only_live_payloads() -> None:
    usage_reports = []
    port = _port(usage_observer=lambda _request, report: usage_reports.append(report))
    binding = port._registry.get("openai_chat_completions")
    binding._adapter._mock_chunks = [
        {"choices": [{"delta": {"content": "hello"}}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 4,
                "prompt_cache_hit_tokens": 3,
                "prompt_cache_miss_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 5,
            },
        },
    ]
    request, _tool_port = _prepared_execution(port)

    async def collect() -> list[object]:
        return [item async for item in port.stream(request)]

    values = asyncio.run(collect())
    assert [type(value) for value in values] == [
        TextStartPayload,
        TextDeltaPayload,
        TextEndPayload,
    ]
    assert not any("Draft" in type(value).__name__ for value in values)
    assert len(usage_reports) == 1
    assert usage_reports[0].usage is not None
    assert usage_reports[0].usage.input_tokens == 4
    assert usage_reports[0].usage.cached_input_tokens == 3
    assert usage_reports[0].usage.output_tokens == 1
    request.surface_borrow.close()
