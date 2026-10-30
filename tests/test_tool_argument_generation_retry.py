"""Unaccepted malformed tool generations share the physical request retry budget."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.llm.normalized_transport import NormalizedProviderTransportExecution
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import resolve_model_call
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.ports.live_agent_event import (
    ToolCallEndPayload,
    ToolCallStartPayload,
)
from pulsara_agent.ports.provider_stream import (
    ProviderGenerationRetry,
    ProviderNormalizedTerminalKind,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.support.model_config import test_model_binding, test_model_runtime


def _chunk(delta, finish_reason=None, **kwargs):
    return {
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        **kwargs,
    }


def _tool_chunks(arguments, *, text="", usage=None):
    chunks = []
    if text:
        chunks.append(
            _chunk({"content": text, "reasoning_content": f"reasoning:{text}"})
        )
    chunks.append(
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call:reused-by-provider",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": arguments},
                    }
                ]
            }
        )
    )
    chunks.append(_chunk({}, "tool_calls"))
    if usage is not None:
        chunks.append({"choices": [], "usage": usage})
    return chunks


class _SDKStream:
    def __init__(self, chunks, *, close_error=None):
        self.chunks = iter(chunks)
        self.close_error = close_error
        self.closed = False
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = next(self.chunks, None)
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class _SDKCompletions:
    def __init__(self, attempts):
        self.attempts = attempts
        self.payloads = []

    async def create(self, **kwargs):
        index = len(self.payloads)
        # A replacement request must never overlap an unsettled SDK stream.
        for previous in self.attempts[:index]:
            if isinstance(previous, _SDKStream):
                assert previous.closed
        self.payloads.append(deepcopy(kwargs))
        attempt = self.attempts[index]
        if isinstance(attempt, BaseException):
            raise attempt
        return attempt


def _execution(
    attempts,
    *,
    purpose=ModelCallPurpose.AGENT_MODEL_LOOP,
    enabled=True,
    api_key="sk-fixture-secret",
):
    runtime = test_model_runtime(wire_api="openai_chat_completions", api_key=api_key)
    binding = test_model_binding(runtime)
    timeout = OpenAITransportTimeoutPolicy(1, 1, 1, 1, None)
    target = runtime.resolve_target(binding, timeout_policy=timeout)
    call = resolve_model_call(target=target, binding=binding, purpose=purpose)
    context = LLMContext(
        messages=(LLMMessage.user("Create the requested file once."),),
        tools=(
            ToolSpec(
                "write_file",
                "Create a file",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            ),
        ),
        context_id="test:generation-retry",
        resolved_model_call_id=call.resolved_model_call_id,
        model_call_index=0 if purpose is ModelCallPurpose.AGENT_MODEL_LOOP else None,
    )
    completions = _SDKCompletions(attempts)
    adapter = target.transport._adapter
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    adapter.retry_config = LLMRetryConfig(enabled=enabled, attempts=3, jitter_ratio=0)
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    adapter.retry_sleep = sleep
    return target.transport.open_stream(call=call, context=context), completions, sleeps


async def _drain(execution):
    output = []
    while (item := await execution.read_next()) is not None:
        output.append(item)
    return output


def _terminal(output):
    assert isinstance(output[-1], ProviderStreamTerminal)
    assert sum(isinstance(item, ProviderStreamTerminal) for item in output) == 1
    return output[-1]


def test_network_then_malformed_then_success_share_budget_and_preserve_input():
    malformed = _SDKStream(_tool_chunks('{"path":', text="DISCARDED"))
    accepted = _SDKStream(_tool_chunks('{"path":"accepted.txt"}', text="ACCEPTED"))
    execution, completions, sleeps = _execution(
        [
            ConnectionError("network unavailable"),
            malformed,
            accepted,
        ]
    )
    output = asyncio.run(_drain(execution))
    terminal = _terminal(output)
    assert terminal.terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert len(completions.payloads) == 3
    assert completions.payloads[0] == completions.payloads[1] == completions.payloads[2]
    assert sleeps == [0.5, 1.0]
    assert malformed.close_calls == accepted.close_calls == 1
    assert sum(isinstance(item, ProviderGenerationRetry) for item in output) == 2
    assert sum(isinstance(item, ToolCallStartPayload) for item in output) == 2
    ends = [item for item in output if isinstance(item, ToolCallEndPayload)]
    assert len(ends) == 1
    assert ends[0].arguments_json == '{"path":"accepted.txt"}'
    assert terminal.completed_replay_payload is not None
    replay = thaw_json(terminal.completed_replay_payload.ordered_items[0])
    assert replay["content"] == "ACCEPTED"
    assert replay["tool_calls"][0]["function"]["arguments"] == '{"path":"accepted.txt"}'


@pytest.mark.parametrize("arguments", ['{"path":', '["not", "an object"]'])
def test_repeated_malformed_generations_exhaust_the_existing_budget(arguments):
    streams = [_SDKStream(_tool_chunks(arguments)) for _ in range(3)]
    execution, completions, sleeps = _execution(streams)
    output = asyncio.run(_drain(execution))
    terminal = _terminal(output)
    assert terminal.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    assert len(completions.payloads) == 3
    assert len(sleeps) == 2
    assert all(stream.closed and stream.close_calls == 1 for stream in streams)
    assert sum(isinstance(item, ProviderGenerationRetry) for item in output) == 2
    assert not any(isinstance(item, ToolCallEndPayload) for item in output)
    assert terminal.error is not None
    summary = terminal.error.retry_summary
    assert summary is not None
    assert summary.final_attempt == summary.max_attempts == 3
    assert summary.retry_count == 2
    assert summary.exhausted


@pytest.mark.parametrize(
    "purpose,enabled",
    [
        (ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY, True),
        (ModelCallPurpose.AGENT_MODEL_LOOP, False),
    ],
)
def test_malformed_generation_requires_agent_purpose_and_enabled_retries(
    purpose, enabled
):
    stream = _SDKStream(_tool_chunks('{"path":'))
    execution, completions, sleeps = _execution(
        [stream], purpose=purpose, enabled=enabled
    )
    output = asyncio.run(_drain(execution))
    assert (
        _terminal(output).terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    )
    assert len(completions.payloads) == 1
    assert sleeps == []
    assert stream.closed
    assert not any(isinstance(item, ProviderGenerationRetry) for item in output)


@pytest.mark.parametrize(
    "chunks",
    [
        [_chunk({"content": "partial"})],
        [
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "missing-name",
                            "function": {"arguments": "{}"},
                        }
                    ]
                }
            ),
            _chunk({}, "tool_calls"),
        ],
    ],
)
def test_other_output_failures_do_not_regenerate(chunks):
    stream = _SDKStream(chunks)
    execution, completions, sleeps = _execution([stream])
    output = asyncio.run(_drain(execution))
    assert (
        _terminal(output).terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    )
    assert len(completions.payloads) == 1
    assert sleeps == []
    assert stream.closed
    assert not any(isinstance(item, ProviderGenerationRetry) for item in output)


@pytest.mark.parametrize("chunks", [_tool_chunks('{"path":'), []])
def test_failed_stream_close_prevents_another_request(chunks):
    stream = _SDKStream(chunks, close_error=ConnectionError("stream close failed"))
    execution, completions, sleeps = _execution([stream])
    output = asyncio.run(_drain(execution))
    assert (
        _terminal(output).terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR
    )
    assert len(completions.payloads) == 1
    assert sleeps == []
    assert stream.close_calls == 1
    assert not any(isinstance(item, ProviderGenerationRetry) for item in output)


def test_cancellation_closes_current_stream_without_regenerating():
    stream = _SDKStream([_chunk({"content": "partial"}), asyncio.CancelledError()])
    execution, completions, sleeps = _execution([stream])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_drain(execution))
    assert stream.closed
    assert stream.close_calls == 1
    assert len(completions.payloads) == 1
    assert sleeps == []


def test_invalid_argument_diagnostics_scrub_escaped_credentials(caplog):
    secret = 'synthetic-secret-"quoted"\\slash\nline-密钥'
    malformed_arguments = (
        json.dumps({"content": secret}, ensure_ascii=True)[:-1] + ', "path":'
    )
    chunks = _tool_chunks(malformed_arguments)
    chunks[-1]["choices"][0]["delta"]["content"] = secret
    invalid = _SDKStream(chunks)
    accepted = _SDKStream(_tool_chunks('{"path":"accepted.txt"}'))
    execution, completions, _ = _execution([invalid, accepted], api_key=secret)
    output = asyncio.run(_drain(execution))
    assert _terminal(output).terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert len(completions.payloads) == 2
    messages = [record.getMessage() for record in caplog.records]
    diagnostic_messages = [message for message in messages if "diagnostic=" in message]
    assert len(diagnostic_messages) == 1
    diagnostic = json.loads(diagnostic_messages[0].split("diagnostic=", 1)[1])
    assert diagnostic["terminal_chunk"]["choices"][0]["delta"]["content"] != secret
    arguments = diagnostic["arguments"]["call:reused-by-provider"]
    # The remaining malformed suffix is useful evidence, while even a nested
    # JSON parse cannot reconstruct the configured credential from the log.
    decoded_prefix = json.loads(arguments.split(', "path":', 1)[0] + "}")
    assert decoded_prefix["content"] != secret
    assert '"path":' in arguments
    assert diagnostic["parse_error"]
    assert "synthetic-secret" not in "\n".join(messages)


def test_saved_settings_are_read_once_before_retry(monkeypatch):
    from tests.support.model_config import StaticTestSettingsStore

    invalid = _SDKStream(_tool_chunks('{"path":'))
    accepted = _SDKStream(_tool_chunks('{"path":"accepted.txt"}'))
    execution, completions, _ = _execution([invalid, accepted])
    read = StaticTestSettingsStore.read
    observations = []
    def read_once(store):
        observations.append(store)
        if len(observations) > 1:
            raise AssertionError("retry re-read saved settings")
        return read(store)
    monkeypatch.setattr(StaticTestSettingsStore, "read", read_once)
    output = asyncio.run(_drain(execution))
    assert _terminal(output).terminal_kind is ProviderNormalizedTerminalKind.COMPLETED
    assert len(completions.payloads) == 2 and len(observations) == 1
    assert invalid.closed and accepted.closed


def test_normalizer_clears_abandoned_usage_on_generation_retry():
    from pulsara_agent.llm.result import TransportUsageReport

    async def stream():
        yield TransportUsageReport(
            usage_status="missing", usage=None, reported_model_id="discarded-model"
        )
        yield ToolCallStartPayload("call:same", "call:same", "write_file")
        yield ProviderGenerationRetry()
        yield ToolCallStartPayload("call:same", "call:same", "write_file")

    async def scenario():
        execution = NormalizedProviderTransportExecution(stream())
        assert isinstance(await execution.read_next(), ToolCallStartPayload)
        assert execution.observed_usage.reported_model_id == "discarded-model"
        assert isinstance(await execution.read_next(), ProviderGenerationRetry)
        assert execution.observed_usage is None
        assert isinstance(await execution.read_next(), ToolCallStartPayload)
        await execution.aclose()

    asyncio.run(scenario())


@pytest.mark.postgres
def test_host_discards_entire_bad_generation_and_executes_accepted_tool_once(
    tmp_path,
    monkeypatch,
    isolated_migrated_postgres_database,
):
    import psycopg

    import pulsara_agent.conversation_kernel.host as kernel_host
    from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
    from pulsara_agent.conversation_kernel.live import (
        LiveChannelKind,
        LiveSettlementKind,
    )
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.tools.builtins.filesystem import WriteFileTool
    from pulsara_agent.workspace_identity import HostWorkspaceInput

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PULSARA_HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dsn = isolated_migrated_postgres_database.runtime_dsn
    runtime = test_model_runtime(wire_api="openai_chat_completions", postgres_dsn=dsn)
    invalid_chunks = _tool_chunks(
        '{"path":',
        text="DISCARDED_GENERATION",
        usage={
            "prompt_tokens": 999,
            "completion_tokens": 888,
            "total_tokens": 1887,
        },
    )
    # Even a well-formed call preceding a bad sibling remains unaccepted.
    invalid_chunks[1]["choices"][0]["delta"]["tool_calls"].insert(
        0,
        {
            "index": 1,
            "id": "call:must-not-execute",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps(
                    {
                        "target": {"kind": "PATH", "path": "discarded.txt"},
                        "content": "must not exist",
                    }
                ),
            },
        },
    )
    invalid = _SDKStream(invalid_chunks)
    accepted = _SDKStream(
        _tool_chunks(
            json.dumps(
                {
                    "target": {"kind": "PATH", "path": "accepted.txt"},
                    "content": "written once",
                }
            ),
            text="ACCEPTED_GENERATION",
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 22,
                "total_tokens": 33,
            },
        )
    )
    final = _SDKStream([_chunk({"content": "FINISHED"}), _chunk({}, "stop")])
    completions = _SDKCompletions([invalid, accepted, final])
    requests = []
    physical_write_paths = []
    original_write = WriteFileTool.execute

    def observe_write(self, call, **kwargs):
        physical_write_paths.append(call.arguments["target"]["path"])
        return original_write(self, call, **kwargs)

    monkeypatch.setattr(WriteFileTool, "execute", observe_write)

    async def no_wait(_delay):
        pass

    class Model(DirectKernelModelPort):
        def preflight_execution(self, request, **kwargs):
            requests.append(request)
            call = request.prepared_call.call
            adapter = call.target.transport._adapter
            adapter._client = SimpleNamespace(
                chat=SimpleNamespace(completions=completions)
            )
            adapter.retry_sleep = no_wait
            runtime.register_test_call_override(call)
            return super().preflight_execution(request, **kwargs)

    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", Model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ()
    )

    async def scenario():
        core = kernel_host.KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
            )
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await asyncio.wait_for(
                session.run_turn(
                    PromptContent.text(
                        "Create accepted.txt once and report completion."
                    ),
                    requested_permission_mode=PermissionMode.ACCEPT_EDITS,
                ),
                timeout=20,
            )
            assert result.final_text == "FINISHED"
            assert not (workspace / "discarded.txt").exists()
            assert (workspace / "accepted.txt").read_text() == "written once"
            assert physical_write_paths == ["accepted.txt"]
            assert len(requests) == 2
            assert len(completions.payloads) == 3
            assert completions.payloads[0] == completions.payloads[1]
            first, following = completions.payloads[0], completions.payloads[2]
            assert first["tools"] == following["tools"]
            assert following["messages"][: len(first["messages"])] == first["messages"]
            assert "DISCARDED_GENERATION" not in json.dumps(following)
            assert "ACCEPTED_GENERATION" in json.dumps(following)
            _, snapshot = session.live_bus.subscribe_with_snapshot()
            settlements = [
                item
                for item in snapshot.settlements
                if item.channel_kind is LiveChannelKind.MODEL_OUTPUT
            ]
            assert [item.kind for item in settlements] == [
                LiveSettlementKind.ABORTED,
                LiveSettlementKind.COMMITTED,
                LiveSettlementKind.COMMITTED,
            ]
            assert len({item.draft_identity for item in settlements}) == 3
            assert len({item.generation_id for item in settlements}) == 3
            abandoned = settlements[0]
            assert abandoned.committed_entry_id is None
            assert settlements[1].committed_entry_id == settlements[1].draft_identity
            with psycopg.connect(dsn) as connection:
                assert connection.execute(
                    "SELECT tool_call_id FROM pulsara_v3.tool_execution_attempts WHERE session_id=%s",
                    (session.session_id,),
                ).fetchall() == [("call:reused-by-provider",)]
                assert connection.execute(
                    "SELECT tool_call_id FROM pulsara_v3.tool_results WHERE session_id=%s",
                    (session.session_id,),
                ).fetchall() == [("call:reused-by-provider",)]
                entries = connection.execute(
                    "SELECT id, inline_content FROM pulsara_v3.transcript_entries WHERE session_id=%s",
                    (session.session_id,),
                ).fetchall()
                assert abandoned.draft_identity not in {row[0] for row in entries}
                assert all(
                    b"DISCARDED_GENERATION" not in bytes(row[1] or b"")
                    for row in entries
                )
                replay = connection.execute(
                    "SELECT assistant_entry_id, payload_bytes FROM pulsara_v3.provider_assistant_replay_fragments WHERE session_id=%s",
                    (session.session_id,),
                ).fetchall()
                assert len(replay) == 1
                assert replay[0][0] == settlements[1].draft_identity
                assert b"DISCARDED_GENERATION" not in bytes(replay[0][1])
                assert b"ACCEPTED_GENERATION" in bytes(replay[0][1])
                assert connection.execute(
                    "SELECT model_call_index, input_tokens, output_tokens FROM pulsara_v3.provider_call_usage WHERE session_id=%s ORDER BY model_call_index",
                    (session.session_id,),
                ).fetchall() == [(1, 11, 22), (2, None, None)]
        finally:
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())
