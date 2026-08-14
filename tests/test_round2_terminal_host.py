from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shlex
import sys
from time import monotonic
from typing import AsyncIterator

import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.direct_model import KernelModelExecutionRequest
from pulsara_agent.llm.input import MessageRole
from pulsara_agent.model_input.lowering import decode_tool_result_observation
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.settings import PulsaraSettings, StorageConfig
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_llm_config
from tests.support.round3 import CallbackScriptedKernelModel


pytestmark = pytest.mark.postgres


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


class _TerminalMonitorDogfoodModel:
    def __init__(self, command: str) -> None:
        self.command = command
        self.autonomous_seen = asyncio.Event()
        self._delegate = CallbackScriptedKernelModel(self._stream)
        self.requests = self._delegate.requests

    def prepare_call(self, request):
        return self._delegate.prepare_call(request)

    def preflight_execution(self, request, **kwargs):
        return self._delegate.preflight_execution(request, **kwargs)

    async def _stream(
        self, request: KernelModelExecutionRequest
    ) -> AsyncIterator[object]:
        call_index = len(self.requests)
        if call_index == 1:
            payloads = _tool_call(
                "terminal",
                "call:round2-terminal",
                {
                    "command": self.command,
                    "yield_time_ms": 0,
                    "max_output_chars": 4000,
                },
            )
        elif call_index == 2:
            process_id = None
            for item in reversed(request.compiled_input.messages):
                if item.role is not MessageRole.TOOL_RESULT or not item.content:
                    continue
                provider_result = decode_tool_result_observation(item.content[0])
                decoded = json.loads(str(provider_result["body"]))
                candidate = decoded.get("process_id")
                if isinstance(candidate, str):
                    process_id = candidate
                    break
            assert process_id is not None
            payloads = _tool_call(
                "terminal_monitor",
                "call:round2-monitor",
                {"action": "register", "process_id": process_id},
            )
        elif call_index == 3:
            payloads = _text("text:round2-waiting", "MONITOR_REGISTERED_WAITING")
        else:
            terminal_items = tuple(
                json.loads(item.content[0])["pulsara_terminal_observation"]
                for item in request.compiled_input.messages
                if item.role is MessageRole.USER
                and item.content
                and item.content[0].startswith(
                    '{"pulsara_terminal_observation":'
                )
            )
            assert len(terminal_items) == 1
            assert "R2_COMPLETION_SENTINEL" in terminal_items[0]["output"]
            self.autonomous_seen.set()
            payloads = _text("text:round2-autonomous", "AUTONOMOUS_COMPLETION_SEEN")
        for payload in payloads:
            yield payload


def test_round2_host_yield_monitor_completion_and_autonomous_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    script = (
        "import time; "
        "print('R2_INITIAL_SENTINEL', flush=True); "
        "time.sleep(1.0); "
        "print('R2_COMPLETION_SENTINEL', flush=True)"
    )
    command = f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}"
    model = _TerminalMonitorDogfoodModel(command)
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host, "load_mcp_server_configs", lambda **_: ())
    monkeypatch.setenv("PULSARA_TERMINAL_SHELL_SNAPSHOT", "0")
    settings = PulsaraSettings(
        llm=test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        ),
        storage=StorageConfig(
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
        ),
    )

    async def scenario() -> None:
        core = KernelHostCore.production(settings=settings)
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=tmp_path,
            )
        )
        first = await session.run_turn("run and monitor the process")
        assert first.final_text == "MONITOR_REGISTERED_WAITING"
        await asyncio.wait_for(model.autonomous_seen.wait(), timeout=8)

        deadline = monotonic() + 5
        while True:
            rows = await asyncio.to_thread(
                session.repository.rehydrate_session,
                session_id=session.session_id,
                deadline_monotonic=monotonic() + 5,
            )
            if any(
                row.get("entry_kind") == "ASSISTANT_MESSAGE"
                and row.get("block_inline_content") == b"AUTONOMOUS_COMPLETION_SEEN"
                for row in rows
            ):
                break
            assert monotonic() < deadline
            await asyncio.sleep(0.05)
        kinds = [str(row["entry_kind"]) for row in rows]
        assert kinds.count("TERMINAL_OBSERVATION") == 1
        observation_index = kinds.index("TERMINAL_OBSERVATION")
        assert kinds[observation_index + 1] == "ASSISTANT_MESSAGE"
        events = await asyncio.to_thread(
            session.repository.events_after,
            session_id=session.session_id,
            after_sequence=0,
            limit=64,
            deadline_monotonic=monotonic() + 5,
        )
        assert (
            sum(
                event["event_type"] == "TerminalObservationAccepted" for event in events
            )
            == 1
        )
        await core.close_session(
            session.host_session_id,
            close_conversation=True,
        )
        await core.shutdown()

    asyncio.run(scenario())
