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
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
    ProviderInputContinuityConflict,
)
from pulsara_agent.conversation_kernel.runner import _prepared_append_candidate
from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport,
    build_chat_completions_payload,
)
from pulsara_agent.llm.adapters.openai.responses import (
    OpenAIResponsesTransport,
    build_responses_payload,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.input import LLMMessage, LLMToolCall
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.continuity import (
    FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    FrozenProviderInputAppendCompileResult,
    NoNewTriggerAnchor,
    ProcessLocalCanonicalFrontier,
    ProviderInputEpochCompatibility,
    ProviderInputContinuityScope,
)
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
from pulsara_agent.ports.provider_stream import ProviderStreamFailure
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.context import context_fingerprint
from tests.support.model_config import test_llm_config
from tests.support.round3 import (
    StaticContextSourceCollector,
    StructuredToolPort,
    static_canonical_compile_facts,
)


def test_round5_foreground_model_rejects_a_total_transport_timeout() -> None:
    with pytest.raises(ValueError, match="must not have a total"):
        DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            ),
            timeout_policy=OpenAITransportTimeoutPolicy(1, 1, 1, 1, 30),
        )


def test_round5_preflight_rejects_a_foreign_transport_timeout_binding() -> None:
    config = test_llm_config(
        api_key="test",
        base_url="https://example.invalid/v1",
        pro_model="test-pro",
        flash_model="test-flash",
        api="openai_chat_completions",
    )
    first = DirectKernelModelPort(
        config=config,
        timeout_policy=OpenAITransportTimeoutPolicy(120, 120, 120, 600, None),
    )
    second = DirectKernelModelPort(
        config=config,
        timeout_policy=OpenAITransportTimeoutPolicy(120, 120, 120, 601, None),
    )
    request, _tool_port = _prepared_execution(first)
    owner, candidate = _continuity_candidate(request)

    with pytest.raises(ValueError, match="does not exact-join preparation"):
        second.preflight_execution(
            request,
            expected_append_candidate_fingerprint=candidate.candidate_fingerprint,
            install_authority=owner.install_authority,
        )
    request.surface_borrow.close()


class _Round5RetryEndpoint:
    def __init__(self, *, api: str, semantic_output_before_failure: bool) -> None:
        self.api = api
        self.semantic_output_before_failure = semantic_output_before_failure
        self.calls = 0

    async def create(self, **_kwargs: object):
        self.calls += 1
        attempt = self.calls
        if attempt == 1 and not self.semantic_output_before_failure:
            raise ConnectionError("transient connection failure before output")

        async def stream():
            if self.api == "openai_chat_completions":
                yield {
                    "model": "test-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"attempt-{attempt}"},
                            "finish_reason": None,
                        }
                    ],
                }
            else:
                yield {
                    "type": "response.output_text.delta",
                    "delta": f"attempt-{attempt}",
                }
            if attempt == 1:
                raise ConnectionError("transient connection failure after output")

        return stream()


class _Round5RetryClient:
    def __init__(self, endpoint: _Round5RetryEndpoint) -> None:
        self.chat = SimpleNamespace(completions=endpoint)
        self.responses = endpoint


async def _no_retry_delay(_seconds: float) -> None:
    return


@pytest.mark.parametrize(
    ("api", "transport_type"),
    (
        ("openai_chat_completions", OpenAIChatCompletionsTransport),
        ("openai_responses", OpenAIResponsesTransport),
    ),
)
def test_round5_provider_retries_before_semantic_output(
    api: str,
    transport_type,
) -> None:
    port = _port(api=api)
    request, _tool_port = _prepared_execution(port)
    owner, candidate = _continuity_candidate(request)
    execution = port.preflight_execution(
        request,
        expected_append_candidate_fingerprint=candidate.candidate_fingerprint,
        install_authority=owner.install_authority,
    )
    endpoint = _Round5RetryEndpoint(
        api=api,
        semantic_output_before_failure=False,
    )
    transport = transport_type(
        api_key="test",
        timeout_policy=OpenAITransportTimeoutPolicy(1, 1, 1, 1, None),
        retry_config=LLMRetryConfig(
            attempts=2,
            base_delay_seconds=0.001,
            max_delay_seconds=0.001,
            jitter_ratio=0,
        ),
        retry_sleep=_no_retry_delay,
        _client=_Round5RetryClient(endpoint),
    )

    async def collect() -> list[object]:
        return [
            item
            async for item in transport.stream(
                call=request.prepared_call.call,
                context=execution.final_context,
            )
        ]

    items = asyncio.run(collect())
    assert endpoint.calls == 2
    assert any(isinstance(item, TextDeltaPayload) for item in items)
    assert not any(isinstance(item, ProviderStreamFailure) for item in items)
    request.surface_borrow.close()


@pytest.mark.parametrize(
    ("api", "transport_type"),
    (
        ("openai_chat_completions", OpenAIChatCompletionsTransport),
        ("openai_responses", OpenAIResponsesTransport),
    ),
)
def test_round5_provider_never_retries_after_semantic_output(
    api: str,
    transport_type,
) -> None:
    port = _port(api=api)
    request, _tool_port = _prepared_execution(port)
    owner, candidate = _continuity_candidate(request)
    execution = port.preflight_execution(
        request,
        expected_append_candidate_fingerprint=candidate.candidate_fingerprint,
        install_authority=owner.install_authority,
    )
    endpoint = _Round5RetryEndpoint(
        api=api,
        semantic_output_before_failure=True,
    )
    transport = transport_type(
        api_key="test",
        timeout_policy=OpenAITransportTimeoutPolicy(1, 1, 1, 1, None),
        retry_config=LLMRetryConfig(
            attempts=2,
            base_delay_seconds=0.001,
            max_delay_seconds=0.001,
            jitter_ratio=0,
        ),
        retry_sleep=_no_retry_delay,
        _client=_Round5RetryClient(endpoint),
    )

    async def collect() -> list[object]:
        return [
            item
            async for item in transport.stream(
                call=request.prepared_call.call,
                context=execution.final_context,
            )
        ]

    items = asyncio.run(collect())
    assert endpoint.calls == 1
    assert any(isinstance(item, TextDeltaPayload) for item in items)
    failures = [item for item in items if isinstance(item, ProviderStreamFailure)]
    assert len(failures) == 1
    assert failures[0].retry_summary is not None
    assert failures[0].retry_summary.skipped_reason == "semantic_output_started"
    request.surface_borrow.close()


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
        initial_entry_id="entry:initial",
        context_binding_revision_id=revision_id,
        provider_input_through_sequence=sequence,
        conversation_scope_kind=scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            session_id=session_id,
            turn_id=turn_id,
            initial_entry_id="entry:initial",
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
    canonical_facts = static_canonical_compile_facts(snapshot)
    sources = StaticContextSourceCollector().collect(canonical_facts=canonical_facts)
    compiled = StructuredModelInputCompiler().compile(
        StructuredModelInputCompileRequest(
            context_id="context:test",
            model_call_index=1,
            canonical_input=snapshot,
            canonical_facts=canonical_facts,
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


def _port(
    *,
    usage_observer=None,
    api: str = "openai_chat_completions",
) -> DirectKernelModelPort:
    return DirectKernelModelPort(
        config=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api=api,
        ),
        usage_observer=usage_observer,
    )


async def _collect_preflighted(
    port: DirectKernelModelPort, request: KernelModelExecutionRequest
) -> list[object]:
    owner, append_candidate = _continuity_candidate(request)
    execution = port.preflight_execution(
        request,
        expected_append_candidate_fingerprint=(
            append_candidate.candidate_fingerprint
        ),
        install_authority=owner.install_authority,
    )
    permit = owner.install(
        candidate_fingerprint=append_candidate.candidate_fingerprint,
        execution_fingerprint=execution.execution_fingerprint,
    )
    return [item async for item in execution.open_once(permit)]


def _continuity_candidate(request: KernelModelExecutionRequest):
    identity = request.compiled_input.canonical_input_identity
    scope = ProviderInputContinuityScope(
        session_id=request.session_id,
        scope_kind=identity.conversation_scope_kind,
        scope_subagent_task_id=identity.scope_subagent_task_id,
    )
    owner = HostProviderInputContinuityOwner(session_id=request.session_id)
    frontier = ProcessLocalCanonicalFrontier(
        latest_context_binding_revision_id=identity.context_binding_revision_id,
        context_base_semantic_identity=FULL_HISTORY_CONTEXT_BASE_IDENTITY,
        through_sequence=identity.provider_input_through_sequence,
        ordered_item_fingerprints=(),
    )
    planning = owner.freeze_planning_input(
        scope=scope,
        canonical_frontier=frontier,
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    compatibility = ProviderInputEpochCompatibility(
        compiler_contract_version="test:compiler",
        base_system_semantic_fingerprint=context_fingerprint("test:base", "base"),
        tool_surface_fingerprint=(
            request.prepared_call.tool_surface.model_surface.surface_fingerprint
        ),
        model_target_fingerprint=(
            request.prepared_call.compile_binding.target_fact.target_fingerprint
        ),
        estimator_fingerprint=(
            request.prepared_call.compile_binding.estimator_fingerprint
        ),
        provider_message_lowering_contract="test:lowering",
        context_base_semantic_identity=FULL_HISTORY_CONTEXT_BASE_IDENTITY,
    )
    candidate = _prepared_append_candidate(
        planning=planning,
        compatibility=compatibility,
        compiled_result=FrozenProviderInputAppendCompileResult(
            compiled_input=request.compiled_input,
            canonical_frontier=frontier,
            source_heads=(),
            appended_message_count=len(request.compiled_input.messages),
            reset_reason=None,
        ),
    )
    owner.register(candidate)
    return owner, candidate


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


@pytest.mark.parametrize(
    ("api", "payload_builder", "message_key", "system_key"),
    (
        (
            "openai_chat_completions",
            build_chat_completions_payload,
            "messages",
            None,
        ),
        ("openai_responses", build_responses_payload, "input", "instructions"),
    ),
)
def test_round3_1_adapter_wire_items_preserve_strict_prefix_and_steer_order(
    api,
    payload_builder,
    message_key: str,
    system_key: str | None,
) -> None:
    port = _port(api=api)
    request, _tool_port = _prepared_execution(port)
    candidate = context_fingerprint("test:append-candidate:v1", api)
    owner = HostProviderInputContinuityOwner(session_id=request.session_id)
    execution = port.preflight_execution(
        request,
        expected_append_candidate_fingerprint=candidate,
        install_authority=owner.install_authority,
    )
    first_context = execution.final_context
    first_payload = payload_builder(
        call=request.prepared_call.call, context=first_context
    )

    steer_messages = tuple(
        LLMMessage.user(value) for value in ("steer one", "steer two", "steer three")
    )
    second_context = replace(
        first_context,
        messages=first_context.messages + steer_messages,
        context_id="context:successor",
        model_call_index=2,
    )
    second_payload = payload_builder(
        call=request.prepared_call.call, context=second_context
    )
    before = first_payload[message_key]
    after = second_payload[message_key]
    assert after[: len(before)] == before
    assert len(after) == len(before) + 3
    assert [item["role"] for item in after[-3:]] == ["user", "user", "user"]
    if api == "openai_chat_completions":
        assert [item["content"] for item in after[-3:]] == [
            "steer one",
            "steer two",
            "steer three",
        ]
    else:
        assert [item["content"] for item in after[-3:]] == [
            "steer one",
            "steer two",
            "steer three",
        ]
    assert first_payload.get("tools") == second_payload.get("tools")
    if system_key is None:
        assert first_payload["messages"][0] == second_payload["messages"][0]
    else:
        assert first_payload[system_key] == second_payload[system_key]
    request.surface_borrow.close()


@pytest.mark.parametrize(
    ("api", "payload_builder", "message_key", "system_key"),
    (
        (
            "openai_chat_completions",
            build_chat_completions_payload,
            "messages",
            None,
        ),
        ("openai_responses", build_responses_payload, "input", "instructions"),
    ),
)
def test_round3_1_adapter_preserves_twelve_call_strict_prefix_trajectory(
    api,
    payload_builder,
    message_key: str,
    system_key: str | None,
) -> None:
    port = _port(api=api)
    request, _tool_port = _prepared_execution(port)
    candidate = context_fingerprint(
        "test:append-candidate:v1", {"api": api, "trajectory": "twelve"}
    )
    owner = HostProviderInputContinuityOwner(session_id=request.session_id)
    execution = port.preflight_execution(
        request,
        expected_append_candidate_fingerprint=candidate,
        install_authority=owner.install_authority,
    )
    contexts = [execution.final_context]
    for call_index in range(2, 13):
        previous = contexts[-1]
        if call_index % 3 == 0:
            tool_call_id = f"tool-call:{call_index}"
            suffix = (
                LLMMessage.assistant_turn(
                    text=f"using tool {call_index}",
                    tool_calls=(
                        LLMToolCall(
                            id=tool_call_id,
                            name="read_file",
                            arguments='{"path":"README.md"}',
                        ),
                    ),
                ),
                LLMMessage.tool_result(
                    f"tool result {call_index}", tool_call_id=tool_call_id
                ),
            )
        elif call_index % 3 == 1:
            suffix = (LLMMessage.user(f"steer {call_index}"),)
        else:
            suffix = (LLMMessage.assistant(f"answer {call_index}"),)
        contexts.append(
            replace(
                previous,
                messages=previous.messages + suffix,
                context_id=f"context:trajectory:{call_index}",
                model_call_index=call_index,
            )
        )

    payloads = [
        payload_builder(call=request.prepared_call.call, context=context)
        for context in contexts
    ]
    for previous, current in zip(payloads[:-1], payloads[1:], strict=True):
        old_items = previous[message_key]
        new_items = current[message_key]
        assert new_items[: len(old_items)] == old_items
        assert previous.get("tools") == current.get("tools")
        if system_key is None:
            assert previous["messages"][0] == current["messages"][0]
        else:
            assert previous[system_key] == current[system_key]
    request.surface_borrow.close()


def test_stage2_direct_model_rejects_invalid_compiled_input_before_send() -> None:
    port = _port()
    request, _tool_port = _prepared_execution(port)
    other, _other_tool_port = _prepared_execution(port)
    invalid = replace(request, compiled_input=other.compiled_input)

    async def collect() -> list[object]:
        return await _collect_preflighted(port, invalid)

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


def test_round3_1_open_rejects_forged_same_shape_install_permit() -> None:
    port = _port()
    request, _tool_port = _prepared_execution(port)
    owner, candidate = _continuity_candidate(request)
    execution = port.preflight_execution(
        request,
        expected_append_candidate_fingerprint=candidate.candidate_fingerprint,
        install_authority=owner.install_authority,
    )
    permit = owner.install(
        candidate_fingerprint=candidate.candidate_fingerprint,
        execution_fingerprint=execution.execution_fingerprint,
    )
    forged = replace(permit)

    async def collect() -> list[object]:
        return [item async for item in execution.open_once(forged)]

    with pytest.raises(
        ProviderInputContinuityConflict,
        match="was not issued for this execution",
    ):
        asyncio.run(collect())
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
        return await _collect_preflighted(port, request)

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
        return await _collect_preflighted(port, request)

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
