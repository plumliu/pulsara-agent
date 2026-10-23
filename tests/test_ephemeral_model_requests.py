"""Advisory calls preserve input ownership and settle physical resources."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.direct_model import (
    AcceptedProviderFinalSource,
    DirectKernelModelPort,
    prepare_suggestion_request,
)
from pulsara_agent.llm.ephemeral import (
    EphemeralRequestFailure,
    EphemeralResultStatus,
    execute_ephemeral_request,
    prepare_text_ephemeral_request,
    validate_ephemeral_text,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.provider_replay import (
    build_prepared_durable_provider_assistant_replay,
)
from pulsara_agent.llm.request import (
    provider_assistant_message_public_projection_fingerprint,
)
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    ThinkingDeltaPayload,
    ToolCallStartPayload,
)
from pulsara_agent.ports.provider_stream import (
    ProviderNormalizedTerminalKind,
    ProviderOutputIncompleteReason,
    ProviderPhysicalCompletion,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.context import freeze_json, thaw_json
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.support.model_config import (
    test_model_binding as binding_for,
    test_model_runtime as runtime_for,
)
from tests.test_stage2_direct_model import _port, _prepared_execution


def _title(runtime, timeout):
    return prepare_text_ephemeral_request(
        runtime=runtime,
        binding=binding_for(runtime),
        purpose=ModelCallPurpose.SESSION_TITLE,
        messages=(LLMMessage.user("Explain cache locality"),),
        system_prompt="Only a short title.",
        timeout_policy=timeout,
    )


def _final_source(port, request, text="The accepted answer."):
    final = LLMMessage.assistant(text)
    if request.prepared_call.call.target.fact.wire_api == "openai_chat_completions":
        native = (
            {
                "role": "assistant",
                "content": text,
                "reasoning_content": "opaque accepted reasoning",
            },
        )
    else:
        native = (
            {
                "type": "reasoning",
                "id": "rs_final",
                "encrypted_content": "opaque-native",
                "summary": [],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        )
    replay = build_prepared_durable_provider_assistant_replay(
        session_id=request.session_id,
        workspace_id="workspace:wdl6b3mtrkghwnvjo4an3ekbau",
        assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
        target=port.replay_target(request.prepared_call),
        public_projection_fingerprint=provider_assistant_message_public_projection_fingerprint(
            final
        ),
        ordered_items=tuple(freeze_json(item) for item in native),
    )
    return AcceptedProviderFinalSource(
        request.prepared_call.epoch_call_target.physical_call_target,
        request.compiled_input,
        request.wire_input_plan,
        replay.assistant_entry_id,
        final,
        replay.fragment(),
    )


@pytest.mark.parametrize("api", ("openai_chat_completions", "openai_responses"))
def test_suggestion_exact_prefix_native_final_once_and_main_request_unchanged(api):
    port = _port(api=api)
    request, _ = _prepared_execution(port)
    try:
        source = _final_source(port, request)
        original = request.wire_input_plan.materialization
        prepared = prepare_suggestion_request(
            runtime=port.model_runtime,
            source=source,
            prompt="Which next question?",
            timeout_policy=port.transport_timeout_policy,
        )
        context = prepared.context
        final = context.ephemeral_wire_input.materialization
        assert final.root_policy_value == original.root_policy_value
        assert final.tool_items == original.tool_items
        assert (
            final.ordered_input_items[: len(original.ordered_input_items)]
            == original.ordered_input_items
        )
        suffix = final.ordered_input_items[len(original.ordered_input_items) :]
        assert suffix[:-1] == source.provider_replay.ordered_items
        assert "Which next question?" in str(thaw_json(suffix[-1]))
        assert context.messages[:-2] == request.compiled_input.messages
        assert context.messages[-2] == source.final_message
        assert context.tool_choice == "auto"
        assert thaw_json(final.context_bearing_projection)["tool_choice"] == "auto"
        assert request.wire_input_plan.materialization is original
        assert "Which next question?" not in str(
            thaw_json(original.context_bearing_projection)
        )
        assert prepared.call_target.purpose is ModelCallPurpose.CONVERSATION_SUGGESTION
        assert not port.model_runtime._active_borrows
    finally:
        request.surface_borrow.close()


@pytest.mark.parametrize("api", ("openai_chat_completions", "openai_responses"))
def test_suffix_over_budget_is_rejected_before_provider_open(api):
    port = _port(api=api)
    request, _ = _prepared_execution(port)
    try:
        source = _final_source(port, request)
        input_tokens = request.wire_input_plan.quote.budget_input_tokens
        source = replace(
            source,
            call_target=replace(
                source.call_target,
                input_budget=replace(
                    source.call_target.input_budget,
                    effective_input_budget_tokens=input_tokens,
                ),
            ),
        )
        with pytest.raises(ValueError, match="exceeds model budget"):
            prepare_suggestion_request(
                runtime=port.model_runtime,
                source=source,
                prompt="What follows?",
                timeout_policy=port.transport_timeout_policy,
            )
        assert not port.model_runtime._active_borrows
    finally:
        request.surface_borrow.close()


def _terminal(kind=ProviderNormalizedTerminalKind.COMPLETED):
    return ProviderStreamTerminal(
        kind,
        TransportUsageReport("missing", None),
        incomplete_reason=(
            ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT
            if kind is ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE
            else None
        ),
    )


class _Execution:
    def __init__(self, items, *, block=False, close_gate=None):
        self.items = iter(items)
        self.block = block
        self.entered = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_gate = close_gate
        self.closed = False

    async def read_next(self):
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        return next(self.items, None)

    async def aclose(self):
        self.close_started.set()
        if self.close_gate:
            await self.close_gate.wait()
        self.closed = True

    async def wait_physical_completion(self):
        assert self.closed
        return ProviderPhysicalCompletion(
            ProviderPhysicalCompletionStatus.COMPLETED, None
        )


class _Runtime:
    def __init__(self, execution):
        self.execution, self.opens, self.releases = execution, 0, 0

    def borrow_transport(self, permit):
        permit._consume_once()

        def open_stream(*, context):
            self.opens += 1
            return self.execution

        def close():
            self.releases += 1

        return SimpleNamespace(open_stream=open_stream, close=close)


@pytest.mark.parametrize(
    "purpose",
    (ModelCallPurpose.SESSION_TITLE, ModelCallPurpose.CONVERSATION_SUGGESTION),
)
@pytest.mark.parametrize(
    ("items", "status", "text"),
    [
        (
            [
                TextDeltaPayload("text", "A"),
                TextDeltaPayload("text", " question?"),
                _terminal(),
            ],
            EphemeralResultStatus.COMPLETED,
            "A question?",
        ),
        ([_terminal()], EphemeralResultStatus.COMPLETED, ""),
        (
            [
                TextDeltaPayload("text", "partial"),
                _terminal(ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE),
            ],
            EphemeralResultStatus.OUTPUT_INCOMPLETE,
            "partial",
        ),
        (
            [
                TextDeltaPayload("text", "Do not adopt"),
                ToolCallStartPayload("tool", "call", "read_file"),
            ],
            EphemeralResultStatus.TOOL_CALL,
            "",
        ),
    ],
)
def test_executor_complete_empty_incomplete_and_mixed_tools_close_once(
    purpose, items, status, text
):
    port = _port()
    prepared = _title(port.model_runtime, port.transport_timeout_policy)
    if purpose is ModelCallPurpose.CONVERSATION_SUGGESTION:
        prepared = replace(
            prepared,
            call_target=replace(prepared.call_target, purpose=purpose),
            context=replace(prepared.context, tool_choice="auto"),
        )

    async def run():
        execution = _Execution(items)
        runtime = _Runtime(execution)
        result = await execute_ephemeral_request(runtime=runtime, prepared=prepared)
        assert (result.status, result.text) == (status, text)
        assert execution.closed and runtime.opens == runtime.releases == 1

    asyncio.run(run())


def test_repeated_cancellation_waits_for_physical_close_and_remains_cancellation():
    port = _port()
    prepared = _title(port.model_runtime, port.transport_timeout_policy)

    async def run():
        gate = asyncio.Event()
        execution = _Execution([], block=True, close_gate=gate)
        runtime = _Runtime(execution)
        task = asyncio.create_task(
            execute_ephemeral_request(runtime=runtime, prepared=prepared)
        )
        await execution.entered.wait()
        task.cancel()
        await execution.close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert execution.closed and runtime.releases == 1

    asyncio.run(run())


def test_response_aggregate_counts_reasoning_and_closes(monkeypatch):
    port = _port()
    prepared = _title(port.model_runtime, port.transport_timeout_policy)
    monkeypatch.setattr(
        "pulsara_agent.llm.ephemeral.MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES", 8
    )

    async def run():
        execution = _Execution([ThinkingDeltaPayload("thought", "123456789")])
        runtime = _Runtime(execution)
        with pytest.raises(EphemeralRequestFailure, match="缓冲边界"):
            await execute_ephemeral_request(runtime=runtime, prepared=prepared)
        assert execution.closed and runtime.releases == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  Why?  ", "Why?"),
        ("", None),
        ("a\nb", None),
        ("a\x00b", None),
        ("a\u2028b", None),
    ],
)
def test_adoption_text_validation(value, expected):
    assert validate_ephemeral_text(value) == expected


class _HttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.bodies.append(body)
        if self.server.fail:
            context_exceeded = self.server.fail == "context_exceeded"
            self.send_response(400 if context_exceeded else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            error = (
                {
                    "code": "context_length_exceeded",
                    "message": "maximum context length exceeded",
                }
                if context_exceeded
                else {"message": "temporarily unavailable"}
            )
            self.wfile.write(json.dumps({"error": error}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.path.endswith("chat/completions"):
            chunks = [
                {
                    "id": "chatcmpl_test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "Cache locality"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl_test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test-pro",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
        else:
            chunks = [
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "Cache locality",
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_test",
                        "model": "test-pro",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {"type": "output_text", "text": "Cache locality"}
                                ],
                            }
                        ],
                    },
                },
            ]
        for item in chunks:
            self.wfile.write(f"data: {json.dumps(item)}\n\n".encode())
        if self.path.endswith("chat/completions"):
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.mark.parametrize("api", ("openai_chat_completions", "openai_responses"))
@pytest.mark.parametrize("fail", (False, True, "context_exceeded"))
@pytest.mark.parametrize(
    "purpose",
    (ModelCallPurpose.SESSION_TITLE, ModelCallPurpose.CONVERSATION_SUGGESTION),
)
def test_saved_advisory_uses_real_sdk_http_single_attempt_and_closes(
    api, fail, purpose
):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HttpHandler)
    server.bodies, server.fail = [], fail
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = runtime_for(
        api_key="sk-fixture-secret",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        model_id="test-pro",
        wire_api=api,
        retry=LLMRetryConfig(enabled=True, attempts=3),
    )
    port = DirectKernelModelPort(model_runtime=runtime)
    request = None
    if purpose is ModelCallPurpose.SESSION_TITLE:
        prepared = _title(runtime, port.transport_timeout_policy)
    else:
        request, _ = _prepared_execution(port)
        source = _final_source(port, request)
        prepared = prepare_suggestion_request(
            runtime=runtime,
            source=source,
            prompt="What follows?",
            timeout_policy=port.transport_timeout_policy,
        )
    original_settings = runtime.settings.read()
    try:

        async def run():
            if fail:
                with pytest.raises(EphemeralRequestFailure) as captured:
                    await execute_ephemeral_request(runtime=runtime, prepared=prepared)
                if fail == "context_exceeded":
                    assert captured.value.code == "invalid_request"
                    assert "context_length_exceeded" in captured.value.message
            else:
                result = await execute_ephemeral_request(
                    runtime=runtime, prepared=prepared
                )
                assert result.status is EphemeralResultStatus.COMPLETED
                assert result.text == "Cache locality"

        asyncio.run(run())
        assert len(server.bodies) == 1
        assert runtime.settings.read() == original_settings
        assert runtime.retry.enabled and not runtime._active_borrows
        if purpose is ModelCallPurpose.SESSION_TITLE:
            assert "tools" not in server.bodies[0]
            assert "tool_choice" not in server.bodies[0]
        else:
            assert server.bodies[0]["tools"]
            assert server.bodies[0]["tool_choice"] == "auto"
            assert "opaque" in json.dumps(server.bodies[0])
        assert prepared.call_target.input_budget.effective_output_tokens > 64
    finally:
        if request is not None:
            request.surface_borrow.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_title_snapshot_keeps_accepted_target_and_saved_drift_rejects_before_open():
    port = _port()
    runtime = port.model_runtime
    snapshot = runtime.freeze_resolution_snapshot()
    original = runtime.settings.read()
    connection = original.model_connections[0]
    runtime.settings.value = replace(
        original,
        model_connections=(replace(connection, base_url="https://changed.invalid/v1"),),
    )
    prepared = prepare_text_ephemeral_request(
        runtime=runtime,
        binding=binding_for(runtime),
        purpose=ModelCallPurpose.SESSION_TITLE,
        messages=(LLMMessage.user("A title"),),
        system_prompt="Only title",
        timeout_policy=port.transport_timeout_policy,
        resolution_snapshot=snapshot,
    )
    assert (
        prepared.call_target.target_bundle.target_fact.canonical_endpoint_base_url
        == connection.base_url
    )

    async def run():
        with pytest.raises(Exception, match="reproduce|differs"):
            await execute_ephemeral_request(runtime=runtime, prepared=prepared)

    asyncio.run(run())
    assert not runtime._active_borrows


def test_title_wire_bytes_admitted_before_borrow(monkeypatch):
    port = _port()
    monkeypatch.setattr(
        "pulsara_agent.llm.request.MAXIMUM_PROVIDER_WIRE_INPUT_BYTES", 16
    )
    with pytest.raises(ValueError, match="physical byte bound"):
        _title(port.model_runtime, port.transport_timeout_policy)
    assert not port.model_runtime._active_borrows


def test_missing_terminal_is_failure_and_physically_closed():
    port = _port()
    prepared = _title(port.model_runtime, port.transport_timeout_policy)

    async def run():
        execution = _Execution([TextDeltaPayload("text", "partial")])
        runtime = _Runtime(execution)
        with pytest.raises(EphemeralRequestFailure, match="协议终态"):
            await execute_ephemeral_request(runtime=runtime, prepared=prepared)
        assert execution.closed and runtime.releases == 1

    asyncio.run(run())


@pytest.mark.parametrize("api", ("openai_chat_completions", "openai_responses"))
def test_suggestion_retains_previous_opaque_native_replay_and_final_native_replay(api):
    from pulsara_agent.llm.input import LLMTextPart
    from pulsara_agent.model_input.contracts import (
        CanonicalInputOriginKind,
        FrozenProviderInputItem,
        FrozenProviderInputItemKind,
    )
    from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
    from pulsara_agent.model_input.provider_replay import (
        freeze_provider_replay_manifest,
        freeze_provider_replay_manifest_cut,
        freeze_selected_provider_replay_hydration,
        select_compatible_provider_replay_manifests,
    )

    old_entry = "entry:blzvcxjwbvctrcbukdihh2ftim"
    port = _port(api=api)
    request, _ = _prepared_execution(
        port,
        canonical_items=(
            FrozenProviderInputItem(
                item_kind=FrozenProviderInputItemKind.ASSISTANT,
                source_entry_id=old_entry,
                source_entry_sequence=1,
                source_turn_id="turn:6jm3rew3jceuvfhjqj2bpxvkhy",
                content=(LLMTextPart("Earlier answer"),),
            ),
            FrozenProviderInputItem(
                item_kind=FrozenProviderInputItemKind.USER,
                source_entry_id="entry:x6eevt4jzgsks3d5pdcognxexm",
                source_entry_sequence=2,
                source_turn_id="turn:klmmazz67vfgsfsvg6ceqgdsfu",
                content=(LLMTextPart("Continue"),),
                input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
            ),
        ),
    )
    try:
        native = (
            (
                {
                    "role": "assistant",
                    "content": "Earlier answer",
                    "reasoning_content": "prior opaque",
                },
            )
            if api == "openai_chat_completions"
            else (
                {
                    "type": "reasoning",
                    "id": "rs_old",
                    "encrypted_content": "prior opaque",
                    "summary": [],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Earlier answer"}],
                },
            )
        )
        target = port.replay_target(request.prepared_call)
        replay = build_prepared_durable_provider_assistant_replay(
            session_id=request.session_id,
            workspace_id="workspace:wdl6b3mtrkghwnvjo4an3ekbau",
            assistant_entry_id=old_entry,
            target=target,
            public_projection_fingerprint=provider_assistant_message_public_projection_fingerprint(
                LLMMessage.assistant("Earlier answer")
            ),
            ordered_items=tuple(freeze_json(item) for item in native),
        )
        manifest = freeze_provider_replay_manifest(
            replay_id=replay.replay_id,
            assistant_entry_id=old_entry,
            wire_api=replay.wire_api,
            codec_kind=replay.codec_kind.value,
            provider_replay_contract_fingerprint=replay.provider_replay_contract_fingerprint,
            replay_target_fingerprint=replay.replay_target_fingerprint,
            public_projection_fingerprint=replay.public_projection_fingerprint,
            payload_digest=replay.payload_digest,
            payload_size=replay.payload_size,
            item_count=replay.item_count,
            fragment_fingerprint=replay.fragment_fingerprint,
        )
        identity = request.compiled_input.canonical_input_identity
        cut = freeze_provider_replay_manifest_cut(
            session_id=request.session_id,
            scope=ProviderInputContinuityScope(
                request.session_id, identity.conversation_scope_kind, None
            ),
            context_binding_revision_id=identity.context_binding_revision_id,
            provider_input_through_sequence=identity.provider_input_through_sequence,
            manifests=(manifest,),
        )
        manifests, placements = select_compatible_provider_replay_manifests(
            manifest_cut=cut,
            compiled_input=request.compiled_input,
            replay_target=target,
        )
        hydration = freeze_selected_provider_replay_hydration(
            manifest_cut=cut,
            compiled_input=request.compiled_input,
            replay_target=target,
            selected_manifests=manifests,
            selected_placements=placements,
            fragments=(replay.fragment(),),
        )
        plan = port.plan_wire_input(
            prepared_call=request.prepared_call,
            compiled_input=request.compiled_input,
            predecessor_view=None,
            replay_hydration=hydration,
        )
        request = replace(request, wire_input_plan=plan)
        source = _final_source(port, request)
        prepared = prepare_suggestion_request(
            runtime=port.model_runtime,
            source=source,
            prompt="What follows?",
            timeout_policy=port.transport_timeout_policy,
        )
        original = plan.materialization.ordered_input_items
        items = (
            prepared.context.ephemeral_wire_input.materialization.ordered_input_items
        )
        assert items[: len(original)] == original
        assert all(items.count(item) == 1 for item in replay.ordered_items)
        assert items[len(original) : -1] == source.provider_replay.ordered_items
        assert "prior opaque" in str(tuple(thaw_json(item) for item in original))
    finally:
        request.surface_borrow.close()


class _ProbeLifecycleHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.server.bodies.append(
            json.loads(self.rfile.read(int(self.headers["content-length"])))
        )
        mode = self.server.mode
        self.send_response(503 if mode == "error" else 200)
        self.send_header(
            "Content-Type",
            "application/json" if mode == "error" else "text/event-stream",
        )
        self.send_header("Connection", "close")
        self.end_headers()
        if mode == "cancel":
            self.server.entered.set()
            self.server.release.wait()
            return
        if mode == "error":
            self.wfile.write(b'{"error":{"message":"unavailable"}}')
            return
        for delta, finish in (
            ({"content": "OK"}, None),
            ({}, "length" if mode == "incomplete" else "stop"),
        ):
            value = {
                "id": "probe",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test-pro",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(f"data: {json.dumps(value)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.mark.parametrize("mode", ("complete", "incomplete", "error", "cancel"))
def test_probe_sdk_http_incomplete_error_cancellation_preserve_contract(mode):
    from threading import Event
    from pulsara_agent.llm.connection_probe import (
        ModelConnectionProbeFailure,
        probe_model_connection,
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeLifecycleHandler)
    server.mode, server.bodies = mode, []
    server.entered, server.release = Event(), Event()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = runtime_for(
        api_key="sk-probe-fixture",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        model_id="test-pro",
        wire_api="openai_chat_completions",
    )
    original = runtime.settings.read()

    async def run():
        probe = probe_model_connection(
            resolved=runtime.freeze_resolution_snapshot().validate(
                binding_for(runtime)
            ),
            catalog=runtime.catalog.selectable(),
            route_wires=runtime.route_wires,
            api_key="sk-probe-fixture",
        )
        if mode == "cancel":
            task = asyncio.create_task(probe)
            await asyncio.to_thread(server.entered.wait)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif mode == "error":
            with pytest.raises(ModelConnectionProbeFailure):
                await probe
        else:
            await probe

    try:
        asyncio.run(run())
        assert len(server.bodies) == 1
        assert server.bodies[0]["max_completion_tokens"] == 64
        assert runtime.settings.read() == original
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join()
