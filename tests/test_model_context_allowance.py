"""A local input allowance must never become a provider/output ceiling."""

from dataclasses import replace

import pytest

from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort,
    KernelModelTargetPreparationRequest,
)
from pulsara_agent.llm.adapters.openai.chat_completions import (
    build_chat_completions_payload,
)
from pulsara_agent.llm.adapters.openai.responses import build_responses_payload
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.model_connections import (
    model_connection_from_dict,
    model_connection_to_dict,
)
from pulsara_agent.llm.model_target import ModelTargetNotExecutable
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.support.model_config import (
    test_model_binding,
    test_model_limits,
    test_model_runtime,
)


def set_allowance(runtime, tokens):
    settings = runtime.settings.read()
    connection = replace(settings.model_connections[0], context_window_tokens=tokens)
    runtime.settings.value = replace(settings, model_connections=(connection,))
    return connection


@pytest.mark.parametrize("wire_api", ("openai_chat_completions", "openai_responses"))
@pytest.mark.parametrize("output", (128_000, 384_000, 1_000_000))
def test_input_allowance_preserves_provider_output_and_hands_over_frozen_budget(
    wire_api, output
):
    runtime = test_model_runtime(
        wire_api=wire_api,
        limits=test_model_limits(
            total_context_tokens=1_000_000,
            max_input_tokens=900_000,
            max_output_tokens=output,
            default_output_tokens=output,
        ),
    )
    port = DirectKernelModelPort(model_runtime=runtime)
    request = KernelModelTargetPreparationRequest(
        session_id="session:test",
        turn_id="turn:test",
        model_call_index=1,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        maximum_input_tokens=None,
        binding=test_model_binding(runtime),
    )
    original = port.prepare_target(request)
    connection = set_allowance(runtime, 256_000)
    assert (
        model_connection_from_dict(model_connection_to_dict(connection)) == connection
    )
    prepared = port.prepare_target(request)
    assert (
        prepared.epoch_call_target.target_bundle
        != original.epoch_call_target.target_bundle
    )
    assert prepared.effective_input_budget_tokens == 256_000 - 1 - 8_192
    assert prepared.target.fact.model_dump(
        exclude={"context_budget", "context_window_tokens"}
    ) == original.target.fact.model_dump(
        exclude={"context_budget", "context_window_tokens"}
    )
    old_target = runtime.resolve_frozen_target_bundle(
        original.epoch_call_target.target_bundle,
        binding=request.binding,
        timeout_policy=port.transport_timeout_policy,
    )
    assert old_target.fact == original.target.fact
    assert (
        old_target.context_budget.input_budget_tokens
        == original.effective_input_budget_tokens
    )
    assert original.effective_input_budget_tokens == 900_000 - 8_192
    assert prepared.target.limits.total_context_tokens == 1_000_000
    assert prepared.target.limits.max_output_tokens == output
    assert prepared.target.context_budget.effective_output_tokens == output
    assert prepared.epoch_call_target.input_budget.effective_output_tokens == output
    context = LLMContext(
        messages=(LLMMessage.user("hello"),),
        context_id="allowance-test",
        resolved_model_call_id=prepared.call.resolved_model_call_id,
        model_call_index=1,
    )
    build = (
        build_chat_completions_payload
        if wire_api == "openai_chat_completions"
        else build_responses_payload
    )
    payload = build(call=prepared.call, context=context)
    field = (
        "max_completion_tokens"
        if wire_api == "openai_chat_completions"
        else "max_output_tokens"
    )
    # The physical shared window still applies, including output == context.
    if output == 1_000_000:
        assert 384_000 < payload[field] <= output
    else:
        assert payload[field] == output
    bounded = port.prepare_target(replace(request, maximum_input_tokens=200_000))
    assert bounded.effective_input_budget_tokens == 200_000
    set_allowance(runtime, 1_000_000)
    assert (
        port.prepare_target(request).epoch_call_target.target_bundle
        == original.epoch_call_target.target_bundle
    )
    set_allowance(runtime, None)
    assert (
        port.prepare_target(request).epoch_call_target.target_bundle
        == original.epoch_call_target.target_bundle
    )


@pytest.mark.parametrize("tokens", (True, 255_999, 256_000.5, "256000"))
def test_input_allowance_rejects_invalid_local_values(tokens):
    with pytest.raises(ValueError, match="allowance"):
        set_allowance(test_model_runtime(), tokens)


def test_frozen_explicit_allowance_survives_a_smaller_saved_allowance():
    runtime = test_model_runtime(
        limits=test_model_limits(
            total_context_tokens=1_000_000,
            max_input_tokens=1_000_000,
            max_output_tokens=384_000,
            default_output_tokens=384_000,
        )
    )
    set_allowance(runtime, 512_000)
    port = DirectKernelModelPort(model_runtime=runtime)
    request = KernelModelTargetPreparationRequest(
        session_id="session:test",
        turn_id="turn:test",
        model_call_index=1,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        maximum_input_tokens=None,
        binding=test_model_binding(runtime),
    )
    original = port.prepare_target(request)
    set_allowance(runtime, 256_000)
    restored = runtime.resolve_frozen_target_bundle(
        original.epoch_call_target.target_bundle,
        binding=request.binding,
        timeout_policy=port.transport_timeout_policy,
    )
    assert restored.fact == original.target.fact
    assert restored.fact.context_window_tokens == 512_000
    assert restored.context_budget.input_budget_tokens == 503_807
    assert port.prepare_target(request).effective_input_budget_tokens == 247_807
    assert restored.context_budget.effective_output_tokens == 384_000

    # Frozen allowance and its derived budget must agree exactly.
    inconsistent = restored.fact.model_dump()
    inconsistent["context_window_tokens"] = 256_000
    with pytest.raises(ValueError, match="pre-margin input budget is inconsistent"):
        type(restored.fact).model_validate(inconsistent)


def test_input_allowance_above_catalog_or_after_catalog_shrink_is_unavailable():
    runtime = test_model_runtime()
    set_allowance(runtime, 256_001)
    with pytest.raises(ModelTargetNotExecutable, match="exceeds catalog"):
        runtime.freeze_resolution_snapshot().validate(test_model_binding(runtime))

    larger = test_model_runtime(
        limits=test_model_limits(
            total_context_tokens=1_000_000, max_input_tokens=1_000_000
        )
    )
    set_allowance(larger, 512_000)
    larger.freeze_resolution_snapshot().validate(test_model_binding(larger))
    larger.catalog._snapshot = test_model_runtime().catalog._snapshot
    with pytest.raises(ModelTargetNotExecutable, match="exceeds catalog"):
        larger.freeze_resolution_snapshot().validate(test_model_binding(larger))


def test_frozen_allowance_reborrow_checks_the_returned_connection_once(monkeypatch):
    from pulsara_agent.llm.model_connections import ModelConnectionAuthentication
    from pulsara_agent.llm.runtime import ModelRuntimeUnavailable

    runtime = test_model_runtime()
    port = DirectKernelModelPort(model_runtime=runtime)
    request = KernelModelTargetPreparationRequest(
        session_id="session:test",
        turn_id="turn:test",
        model_call_index=1,
        purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
        maximum_input_tokens=None,
        binding=test_model_binding(runtime),
    )
    bundle = port.prepare_target(request).epoch_call_target.target_bundle
    original_connection = type(runtime).connection
    reads = []

    def connection_once(owner, binding):
        connection = original_connection(owner, binding)
        reads.append(connection)
        return connection

    monkeypatch.setattr(type(runtime), "connection", connection_once)
    restored = runtime.resolve_frozen_target_bundle(
        bundle,
        binding=request.binding,
        timeout_policy=port.transport_timeout_policy,
    )
    assert reads == [restored.connection]
    reads.clear()
    wrong_auth = replace(
        bundle,
        connection=replace(
            bundle.connection,
            authentication_mode=ModelConnectionAuthentication.NONE,
        ),
    )
    with pytest.raises(ModelRuntimeUnavailable, match="installed epoch contract"):
        runtime.resolve_frozen_target_bundle(
            wrong_auth,
            binding=request.binding,
            timeout_policy=port.transport_timeout_policy,
        )
    assert len(reads) == 1
