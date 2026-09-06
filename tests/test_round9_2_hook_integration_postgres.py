"""Round 9.2 production-composition Hook lifecycle integration gates."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shlex
import sys
import threading
from time import monotonic

from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionDisposition,
)
from pulsara_agent.conversation_kernel.repository import PlanQuestionAnswer
from pulsara_agent.hooks.contracts import HookSourceKind
from pulsara_agent.hooks.source import LocalHookSourceProvider
from pulsara_agent.llm.input import MessageRole
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.model_input.continuity import decode_runtime_observation
from pulsara_agent.model_input.contracts import (
    ContextSourceKind,
    ContextTrustClass,
    ModelInputScopeKind,
)
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
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletion,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import PlanQuestionAnswerKind
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
)
from pulsara_agent.workspace_identity import (
    HostWorkspaceInput,
    resolve_workspace,
)
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.support.round3 import CallbackScriptedKernelModel, ScriptedKernelModel


pytestmark = pytest.mark.postgres


_HOOK_DRIVER = r"""
import json
from pathlib import Path
import sys

label, log_path = sys.argv[1:3]
value = json.loads(sys.stdin.read())
with Path(log_path).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"label": label, "stdin": value}, ensure_ascii=False, separators=(",", ":")) + "\n")

event = value["hook_event_name"]
if event == "SessionStart":
    print(f"SESSION_START_CONTEXT:{label}:{value['source']}")
elif event == "UserPromptSubmit":
    print(f"USER_PROMPT_CONTEXT:{label}:{value['prompt']}")
elif event == "PreToolUse":
    path = value["tool_input"].get("path", "")
    if path == "denied.txt":
        print("PRE_TOOL_BLOCKED", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": f"PRE_TOOL_CONTEXT:{label}"}}))
elif event == "PermissionRequest":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow", "message": "allowed by test Hook"}}}))
elif event == "PostToolUse":
    state = value["tool_response"].get("result_state", "UNKNOWN") if isinstance(value["tool_response"], dict) else "UNKNOWN"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": f"POST_TOOL_CONTEXT:{label}:{state}"}}))
elif event == "SubagentStart":
    print(f"SUBAGENT_START_CONTEXT:{label}:{value['agent_id']}")
elif event == "SubagentStop" and not value["stop_hook_active"]:
    print("SUBAGENT_STOP_CONTINUE_ONCE", file=sys.stderr)
    raise SystemExit(2)
elif event == "SubagentStop":
    print(json.dumps({"continue": False, "stopReason": "SUBAGENT_STOP_TERMINALIZED"}))
elif event == "Stop" and not value["stop_hook_active"]:
    print("STOP_CONTINUE_ONCE", file=sys.stderr)
    raise SystemExit(2)
elif event == "Stop":
    print(json.dumps({"continue": False, "stopReason": "STOP_TERMINALIZED"}))
"""


def _text_stream(text: str, block_id: str) -> list[object]:
    return [
        TextStartPayload(block_id),
        TextDeltaPayload(block_id, text),
        TextEndPayload(
            block_id,
            text,
            len(text.encode("utf-8")),
            live_digest(text),
        ),
    ]


def _tool_stream(
    tool_name: str, tool_call_id: str, arguments: dict[str, object]
) -> list[object]:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return [
        ToolCallStartPayload(tool_call_id, tool_call_id, tool_name),
        ToolCallDeltaPayload(tool_call_id, tool_call_id, encoded),
        ToolCallEndPayload(
            block_identity=tool_call_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_json=encoded,
            utf8_bytes=len(encoded.encode("utf-8")),
            digest=live_digest(encoded),
        ),
    ]


class _CompactionSummaryExecution:
    def __init__(self, text: str) -> None:
        block_id = "compaction-summary:text"
        self._items: list[object] = [
            TextStartPayload(block_id),
            TextDeltaPayload(block_id, text),
            TextEndPayload(
                block_id,
                text,
                len(text.encode("utf-8")),
                live_digest(text),
            ),
            ProviderStreamTerminal(
                terminal_kind=ProviderNormalizedTerminalKind.COMPLETED,
                usage=TransportUsageReport(usage_status="missing", usage=None),
            ),
        ]

    async def read_next(self):
        return self._items.pop(0) if self._items else None

    async def aclose(self) -> None:
        self._items.clear()

    async def wait_physical_completion(self) -> ProviderPhysicalCompletion:
        return ProviderPhysicalCompletion(
            ProviderPhysicalCompletionStatus.COMPLETED,
            None,
        )


class _CompactionSummaryTransport:
    def __init__(self, summary: str) -> None:
        self._summary = summary
        self.open_count = 0

    def open_stream(self, *, call, context):
        del call, context
        self.open_count += 1
        return _CompactionSummaryExecution(self._summary)


class _CompactionScriptedModel(ScriptedKernelModel):
    def __init__(self, calls: list[list[object]], summary: str) -> None:
        super().__init__(calls)
        self.summary_transport = _CompactionSummaryTransport(summary)

    def resolve_compaction_summary_call(self, **kwargs):
        call = super().resolve_compaction_summary_call(**kwargs)
        self.summary_transport.binding_id = call.target.transport.binding_id
        self.summary_transport.contract_version = call.target.transport.contract_version
        return replace(
            call,
            target=replace(call.target, transport=self.summary_transport),
        )


class _ActiveCompactionModel(CallbackScriptedKernelModel):
    def __init__(self) -> None:
        self.active_provider_started = asyncio.Event()
        self.active_provider_release = asyncio.Event()
        self.summary_transport = _CompactionSummaryTransport(
            "Earlier context is complete; continue the active request after the tool result."
        )
        super().__init__(self._stream)

    def resolve_compaction_summary_call(self, **kwargs):
        call = super().resolve_compaction_summary_call(**kwargs)
        self.summary_transport.binding_id = call.target.transport.binding_id
        self.summary_transport.contract_version = call.target.transport.contract_version
        return replace(
            call,
            target=replace(call.target, transport=self.summary_transport),
        )

    async def _stream(self, request):
        del request
        index = len(self.requests)
        if index == 1:
            items = _text_stream("ACTIVE_HISTORY_ONE", "text:active-history:1")
        elif index == 2:
            items = _text_stream("ACTIVE_HISTORY_TWO", "text:active-history:2")
        elif index == 3:
            items = _text_stream("ACTIVE_HISTORY_THREE", "text:active-history:3")
        elif index == 4:
            self.active_provider_started.set()
            await self.active_provider_release.wait()
            items = _tool_stream(
                "read_file",
                "call:active-read",
                {"path": "active-input.txt"},
            )
        elif index == 5:
            items = _text_stream("ACTIVE_COMPACTION_COMPLETE", "text:active-final")
        else:  # pragma: no cover - an extra open is a continuity failure
            raise AssertionError("unexpected active compaction provider open")
        for item in items:
            yield item


class _QueuedContextIsolationModel(CallbackScriptedKernelModel):
    def __init__(self) -> None:
        self.active_provider_started = asyncio.Event()
        self.active_provider_release = asyncio.Event()
        super().__init__(self._stream)

    async def _stream(self, request):
        del request
        index = len(self.requests)
        if index == 1:
            self.active_provider_started.set()
            await self.active_provider_release.wait()
            items = _tool_stream(
                "read_file",
                "call:queued-context-isolation",
                {"path": "queued-input.txt"},
            )
        elif index == 2:
            items = _text_stream("ACTIVE_TURN_A_COMPLETE", "text:queued:A")
        elif index == 3:
            items = _text_stream("QUEUED_TURN_B_COMPLETE", "text:queued:B")
        else:  # pragma: no cover - an extra open is a candidate-binding failure
            raise AssertionError("unexpected queued-context provider open")
        for item in items:
            yield item


class _SubagentHookModel(CallbackScriptedKernelModel):
    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        super().__init__(self._stream)

    async def _stream(self, request):
        identity = request.compiled_input.canonical_input_identity
        if identity.conversation_scope_kind is ModelInputScopeKind.ROOT:
            if request.model_call_index == 1:
                items = _tool_stream(
                    "spawn_agent",
                    "call:round9-2-spawn",
                    {"task": "Exercise child lifecycle Hooks exactly once."},
                )
            elif request.model_call_index == 2:
                deadline = monotonic() + 5
                while True:
                    values = (
                        _log_values(self._log_path) if self._log_path.exists() else []
                    )
                    stop_count = sum(
                        value["stdin"]["hook_event_name"] == "SubagentStop"
                        for value in values
                    )
                    if stop_count == 2:
                        break
                    if monotonic() >= deadline:
                        raise AssertionError(
                            "ROOT final opened before child Hook terminalization"
                        )
                    await asyncio.sleep(0.01)
                items = _text_stream("ROOT_AFTER_CHILD_COMPLETE", "text:root-final")
            else:  # pragma: no cover - an extra ROOT open is a topology failure
                raise AssertionError("unexpected ROOT provider open")
        else:
            assert identity.conversation_scope_kind is ModelInputScopeKind.SUBAGENT_TASK
            if request.model_call_index == 1:
                items = _text_stream("CHILD_FIRST_COMPLETION", "text:child-first")
            elif request.model_call_index == 2:
                items = _text_stream("CHILD_FINAL_COMPLETION", "text:child-final")
            else:  # pragma: no cover - continuation is strictly one-shot
                raise AssertionError("unexpected child provider open")
        for item in items:
            yield item


def _command(driver: Path, log_path: Path, label: str) -> str:
    return " ".join(
        shlex.quote(value)
        for value in (sys.executable, str(driver), label, str(log_path))
    )


def _config(
    command: str,
    events: tuple[str, ...],
    *,
    tool_matcher: str = "write_file",
) -> dict[str, object]:
    def handler(event: str) -> dict[str, object]:
        value: dict[str, object] = {
            "type": "command",
            "command": command,
            "timeout": 3 if event == "SessionEnd" else 5,
        }
        if event in {
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "SubagentStart",
        }:
            value["additionalContextLimit"] = 2048
        return value

    return {
        "description": "Round 9.2 production integration",
        "hooks": {
            event: [
                {
                    "matcher": tool_matcher
                    if "Tool" in event or event == "PermissionRequest"
                    else "*",
                    "hooks": [handler(event)],
                }
            ]
            for event in events
        },
    }


def _install_trusted_sources(
    *,
    home: Path,
    workspace_root: Path,
    user_config: dict[str, object],
    workspace_config: dict[str, object] | None,
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "hooks.json").write_text(
        json.dumps(user_config, ensure_ascii=False), encoding="utf-8"
    )
    if workspace_config is not None:
        workspace_dir = workspace_root / ".pulsara"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "hooks.json").write_text(
            json.dumps(workspace_config, ensure_ascii=False), encoding="utf-8"
        )
    resolved = resolve_workspace(
        HostWorkspaceInput(workspace_kind="project", workspace_root=workspace_root)
    )
    provider = LocalHookSourceProvider(
        workspace_root=resolved.workspace_root,
        workspace_kind=resolved.workspace_kind,
        workspace_state_key=resolved.workspace_key,
        pulsara_home=home,
    )
    view = provider.discover()
    expected = {HookSourceKind.USER_FILE}
    if workspace_config is not None:
        expected.add(HookSourceKind.WORKSPACE_FILE)
    for snapshot in view.source_snapshots:
        if snapshot.provenance.identity.kind not in expected:
            continue
        digest = snapshot.trust.current_definition_digest
        assert digest is not None
        provider.trust_store.trust(
            snapshot.provenance.trust_subject,
            expected_digest=digest,
        )
    trusted = provider.discover()
    assert all(
        snapshot.runnable
        for snapshot in trusted.source_snapshots
        if snapshot.provenance.identity.kind in expected
    )


def _runtime(postgres_dsn: str):
    return test_model_runtime(
        api_key="sk-fixture-secret",
        base_url="https://example.invalid/v1",
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=postgres_dsn,
    )


def _log_values(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _hook_observations(request: object):
    compiled = request.compiled_input  # type: ignore[attr-defined]
    return [
        decode_runtime_observation(message)
        for message in compiled.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in message.content[0]
        and decode_runtime_observation(message).source_kind
        is ContextSourceKind.HOOK_CONTEXT
    ]


def test_round9_2_host_sources_stop_and_prefix_continuity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    driver = tmp_path / "hook_driver.py"
    log_path = tmp_path / "hooks.jsonl"
    driver.write_text(_HOOK_DRIVER, encoding="utf-8")
    user_events = (
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "SessionEnd",
    )
    workspace_events = ("UserPromptSubmit",)
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        user_config=_config(_command(driver, log_path, "USER"), user_events),
        workspace_config=_config(
            _command(driver, log_path, "WORKSPACE"), workspace_events
        ),
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    model = ScriptedKernelModel(
        [
            _text_stream("FIRST_STOP_CANDIDATE", "text:first"),
            _text_stream("FINAL_AFTER_STOP_HOOK", "text:second"),
        ]
    )
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    main_thread_id = threading.get_ident()
    cold_scans: list[tuple[int, float | None]] = []
    original_discover = kernel_host.LocalHookSourceProvider.discover

    def observed_discover(self, *, deadline_monotonic=None):
        cold_scans.append((threading.get_ident(), deadline_monotonic))
        return original_discover(self, deadline_monotonic=deadline_monotonic)

    monkeypatch.setattr(
        kernel_host.LocalHookSourceProvider,
        "discover",
        observed_discover,
    )

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=_runtime(stage2_migrated_postgres_database.runtime_dsn)
        )
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
        )
        await session.update_model_call_binding(
            test_model_binding(core._model_runtime)  # noqa: SLF001
        )
        result = await session.run_turn(
            "exercise source ordering and Stop",
            command_id="command:round9-2-lifecycle",
            requested_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        assert result.final_text == "FINAL_AFTER_STOP_HOOK"
        assert len(model.requests) == 2
        first = model.requests[0].compiled_input
        second = model.requests[1].compiled_input
        assert second.system_prompt == first.system_prompt
        assert second.tools == first.tools
        assert second.messages[: len(first.messages)] == first.messages
        observations = _hook_observations(model.requests[0])
        assert len(observations) == 1
        assert observations[0].trust_class is ContextTrustClass.UNTRUSTED_OBSERVATION
        assert "SESSION_START_CONTEXT:USER" in observations[0].body
        assert observations[0].body.index("USER_PROMPT_CONTEXT:USER") < (
            observations[0].body.index("USER_PROMPT_CONTEXT:WORKSPACE")
        )
        continuation = _hook_observations(model.requests[1])
        assert any("STOP_CONTINUE_ONCE" in item.body for item in continuation)
        await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()

    asyncio.run(scenario())
    assert len(cold_scans) == 1
    assert cold_scans[0][0] != main_thread_id
    assert cold_scans[0][1] is not None
    logs = _log_values(log_path)
    events = [value["stdin"]["hook_event_name"] for value in logs]  # type: ignore[index]
    assert events.count("SessionStart") == 1
    assert events.count("UserPromptSubmit") == 2
    assert events.count("Stop") == 2
    assert events[-1] == "SessionEnd"
    stop_inputs = [
        value["stdin"]
        for value in logs
        if value["stdin"]["hook_event_name"] == "Stop"  # type: ignore[index]
    ]
    assert [value["stop_hook_active"] for value in stop_inputs] == [False, True]


def test_round9_2_queued_prompt_context_waits_for_exact_queue_head_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "queued-input.txt").write_text("queued input", encoding="utf-8")
    driver = tmp_path / "hook_driver.py"
    log_path = tmp_path / "hooks.jsonl"
    driver.write_text(_HOOK_DRIVER, encoding="utf-8")
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        user_config=_config(
            _command(driver, log_path, "USER"),
            ("UserPromptSubmit", "SessionEnd"),
        ),
        workspace_config=None,
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    model = _QueuedContextIsolationModel()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=_runtime(stage2_migrated_postgres_database.runtime_dsn)
        )
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
        )
        await session.update_model_call_binding(
            test_model_binding(core._model_runtime)  # noqa: SLF001
        )
        running = asyncio.create_task(
            session.run_turn(
                "active-turn-A",
                command_id="command:queued-context:A",
                requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
        )
        await asyncio.wait_for(model.active_provider_started.wait(), timeout=5)
        queued = await session.submit_prompt(
            command_id="command:queued-context:B",
            text="queued-turn-B",
            requested_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        assert queued.status == "PENDING"
        model.active_provider_release.set()
        active = await asyncio.wait_for(running, timeout=10)
        assert active.final_text == "ACTIVE_TURN_A_COMPLETE"
        deadline = monotonic() + 10
        while True:
            outcome = await session.query_command("command:queued-context:B")
            assert outcome is not None
            if outcome.status == "SUCCEEDED":
                break
            assert monotonic() < deadline
            await asyncio.sleep(0.01)
        assert len(model.requests) == 3
        first = "\n".join(item.body for item in _hook_observations(model.requests[0]))
        followup = "\n".join(item.body for item in _hook_observations(model.requests[1]))
        delivered = "\n".join(item.body for item in _hook_observations(model.requests[2]))
        assert "USER_PROMPT_CONTEXT:USER:active-turn-A" in first
        assert "USER_PROMPT_CONTEXT:USER:queued-turn-B" not in followup
        assert "USER_PROMPT_CONTEXT:USER:queued-turn-B" in delivered
        await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()

    asyncio.run(scenario())


def test_round9_2_pre_permission_post_real_command_and_canonical_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    driver = tmp_path / "hook_driver.py"
    log_path = tmp_path / "hooks.jsonl"
    driver.write_text(_HOOK_DRIVER, encoding="utf-8")
    events = ("PreToolUse", "PermissionRequest", "PostToolUse", "SessionEnd")
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        user_config=_config(_command(driver, log_path, "USER"), events),
        workspace_config=None,
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    model = ScriptedKernelModel(
        [
            _tool_stream(
                "write_file",
                "call:denied",
                {"path": "denied.txt", "content": "must not exist"},
            ),
            _tool_stream(
                "write_file",
                "call:allowed",
                {"path": "allowed.txt", "content": "allowed"},
            ),
            _text_stream("TOOLS_COMPLETE", "text:final"),
        ]
    )
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=_runtime(stage2_migrated_postgres_database.runtime_dsn)
        )
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
        )
        await session.update_model_call_binding(
            test_model_binding(core._model_runtime)  # noqa: SLF001
        )
        result = await session.run_turn(
            "exercise tool Hooks",
            command_id="command:round9-2-tools",
            requested_permission_mode=PermissionMode.ASK_PERMISSIONS,
        )
        assert result.final_text == "TOOLS_COMPLETE"
        assert not (workspace / "denied.txt").exists()
        assert (workspace / "allowed.txt").read_text(encoding="utf-8") == "allowed"
        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            attempts = connection.execute(
                "SELECT tool_call_id FROM pulsara_v3.tool_execution_attempts "
                "WHERE session_id = %s ORDER BY tool_call_id",
                (session.session_id,),
            ).fetchall()
            results = connection.execute(
                "SELECT tool_call_id, result_state, attempt_id "
                "FROM pulsara_v3.tool_results WHERE session_id = %s "
                "ORDER BY tool_call_id",
                (session.session_id,),
            ).fetchall()
        assert attempts == [{"tool_call_id": "call:allowed"}]
        assert [row["tool_call_id"] for row in results] == [
            "call:allowed",
            "call:denied",
        ]
        assert next(row for row in results if row["tool_call_id"] == "call:denied") == {
            "tool_call_id": "call:denied",
            "result_state": "PERMISSION_DENIED",
            "attempt_id": None,
        }
        final_observations = _hook_observations(model.requests[-1])
        assert any("POST_TOOL_CONTEXT:USER" in item.body for item in final_observations)
        await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()

    asyncio.run(scenario())
    logs = _log_values(log_path)
    tool_logs = [
        value["stdin"]
        for value in logs
        if value["stdin"]["hook_event_name"]
        in {  # type: ignore[index]
            "PreToolUse",
            "PermissionRequest",
            "PostToolUse",
        }
    ]
    assert [value["hook_event_name"] for value in tool_logs] == [
        "PreToolUse",
        "PostToolUse",
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
    ]
    assert all(value["tool_name"] == "apply_patch" for value in tool_logs)
    assert all(value["pulsara_tool_name"] == "write_file" for value in tool_logs)
    assert tool_logs[0]["tool_input"]["path"] == "denied.txt"  # type: ignore[index]
    assert tool_logs[2]["tool_input"]["path"] == "allowed.txt"  # type: ignore[index]
    assert logs[-1]["stdin"]["hook_event_name"] == "SessionEnd"  # type: ignore[index]


def test_round9_2_reload_uses_exact_predecessor_for_own_pre_and_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "after-reload.txt").write_text("new view", encoding="utf-8")
    driver = tmp_path / "hook_driver.py"
    log_path = tmp_path / "hooks.jsonl"
    driver.write_text(_HOOK_DRIVER, encoding="utf-8")
    old_config = _config(
        _command(driver, log_path, "OLD"),
        ("PreToolUse", "PostToolUse"),
        tool_matcher="reload_hooks",
    )
    new_config = _config(
        _command(driver, log_path, "NEW"),
        ("PreToolUse", "PostToolUse", "SessionEnd"),
        tool_matcher="read_file",
    )
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        user_config=old_config,
        workspace_config=None,
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    source_changed = False

    async def stream(request):
        nonlocal source_changed
        index = len(model.requests)
        if index == 1:
            assert not source_changed
            (home / "hooks.json").write_text(
                json.dumps(new_config, ensure_ascii=False), encoding="utf-8"
            )
            resolved = resolve_workspace(
                HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
            )
            provider = LocalHookSourceProvider(
                workspace_root=resolved.workspace_root,
                workspace_kind=resolved.workspace_kind,
                workspace_state_key=resolved.workspace_key,
                pulsara_home=home,
            )
            replacement = provider.discover()
            user = next(
                snapshot
                for snapshot in replacement.source_snapshots
                if snapshot.provenance.identity.kind is HookSourceKind.USER_FILE
            )
            assert user.trust.current_definition_digest is not None
            provider.trust_store.trust(
                user.provenance.trust_subject,
                expected_digest=user.trust.current_definition_digest,
            )
            source_changed = True
            items = _tool_stream("reload_hooks", "call:reload-hooks", {})
        elif index == 2:
            items = _tool_stream(
                "read_file",
                "call:after-reload",
                {"path": "after-reload.txt"},
            )
        elif index == 3:
            items = _text_stream("RELOAD_COMPLETE", "text:reload-final")
        else:  # pragma: no cover - an extra open is a continuity failure
            raise AssertionError("unexpected reload provider open")
        for item in items:
            yield item

    model = CallbackScriptedKernelModel(stream)
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=_runtime(stage2_migrated_postgres_database.runtime_dsn)
        )
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
        )
        await session.update_model_call_binding(
            test_model_binding(core._model_runtime)  # noqa: SLF001
        )
        captured_views: list[tuple[str, object]] = []
        predecessor = session._hooks.current_view  # noqa: SLF001
        original_dispatch = session._hooks.dispatch  # noqa: SLF001

        async def capture_dispatch(envelope, **kwargs):
            captured_views.append(
                (
                    envelope.public_input.event_type.external_name,
                    envelope.definition_view,
                )
            )
            return await original_dispatch(envelope, **kwargs)

        session._hooks.dispatch = capture_dispatch  # type: ignore[method-assign]  # noqa: SLF001
        try:
            result = await session.run_turn(
                "Reload the reviewed Hook source, then read one file.",
                command_id="command:round9-2-reload",
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            assert result.final_text == "RELOAD_COMPLETE"
            successor = session._hooks.current_view  # noqa: SLF001
            assert successor is not predecessor
            tool_views = [
                (event, view)
                for event, view in captured_views
                if event in {"PreToolUse", "PostToolUse"}
            ]
            assert [event for event, _view in tool_views] == [
                "PreToolUse",
                "PostToolUse",
                "PreToolUse",
                "PostToolUse",
            ]
            assert tool_views[0][1] is predecessor
            assert tool_views[1][1] is predecessor
            assert tool_views[2][1] is successor
            assert tool_views[3][1] is successor
            first = model.requests[0].compiled_input
            assert all(
                request.compiled_input.system_prompt == first.system_prompt
                and request.compiled_input.tools == first.tools
                for request in model.requests[1:]
            )
            assert all(
                current.compiled_input.messages[: len(previous.compiled_input.messages)]
                == previous.compiled_input.messages
                for previous, current in zip(
                    model.requests, model.requests[1:], strict=False
                )
            )
        finally:
            await core.close_session(session.host_session_id, close_conversation=True)
            await core.shutdown()

    asyncio.run(scenario())
    logs = _log_values(log_path)
    tool_logs = [
        value
        for value in logs
        if value["stdin"]["hook_event_name"]  # type: ignore[index]
        in {"PreToolUse", "PostToolUse"}
    ]
    assert [value["label"] for value in tool_logs] == [
        "OLD",
        "OLD",
        "NEW",
        "NEW",
    ]
    assert [value["stdin"]["tool_use_id"] for value in tool_logs] == [  # type: ignore[index]
        "call:reload-hooks",
        "call:reload-hooks",
        "call:after-reload",
        "call:after-reload",
    ]
    assert logs[-1]["label"] == "NEW"
    assert logs[-1]["stdin"]["hook_event_name"] == "SessionEnd"  # type: ignore[index]


def test_round9_2_plan_immediate_and_delayed_settlements_share_hook_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    driver = tmp_path / "hook_driver.py"
    log_path = tmp_path / "hooks.jsonl"
    driver.write_text(_HOOK_DRIVER, encoding="utf-8")
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        user_config=_config(
            _command(driver, log_path, "USER"),
            ("PreToolUse", "PostToolUse", "SessionEnd"),
            tool_matcher="write_file|ask_plan_question",
        ),
        workspace_config=None,
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    question = {
        "question": "Which path?",
        "options": [
            {
                "label": "Safe",
                "description": "Keep the atomic Plan owner",
                "recommended": True,
            },
            {
                "label": "Fast",
                "description": "Alternative answer for the closed question schema",
                "recommended": False,
            },
        ],
        "allow_free_text": True,
    }
    first_batch = _tool_stream(
        "write_file",
        "call:barrier-sibling",
        {"path": "plan-sibling-must-not-exist.txt", "content": "blocked"},
    ) + _tool_stream("ask_plan_question", "call:plan-question", question)
    model = ScriptedKernelModel(
        [
            first_batch,
            _text_stream("PLAN_QUESTION_COMPLETE", "text:plan-final"),
        ]
    )
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=_runtime(stage2_migrated_postgres_database.runtime_dsn)
        )
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
        )
        await session.update_model_call_binding(
            test_model_binding(core._model_runtime)  # noqa: SLF001
        )
        entered = await session.enter_plan(
            command_id="command:round9-2-enter-plan",
            entry_reason="exercise delayed Plan settlement",
            resume_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        assert entered.status == "SUCCEEDED"
        running = asyncio.create_task(
            session.run_turn(
                "Ask the planned question",
                command_id="command:round9-2-plan-question",
                requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
        )
        deadline = monotonic() + 5
        opened = None
        while opened is None:
            if running.done():
                await running
            opened = await session._plan_interactions.current_open()  # noqa: SLF001
            assert monotonic() < deadline
            if opened is None:
                await asyncio.sleep(0.01)
        before_answer = _log_values(log_path)
        before_tool = [
            value["stdin"]
            for value in before_answer
            if value["stdin"]["hook_event_name"] in {"PreToolUse", "PostToolUse"}  # type: ignore[index]
        ]
        assert [value["hook_event_name"] for value in before_tool] == [
            "PreToolUse",
            "PostToolUse",
        ]
        assert before_tool[0]["tool_name"] == "ask_plan_question"
        assert before_tool[1]["tool_name"] == "apply_patch"
        assert before_tool[1]["tool_use_id"] == "call:barrier-sibling"
        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            workflow = connection.execute(
                "SELECT workflow_revision FROM pulsara_v3.plan_workflows "
                "WHERE session_id = %s AND id = %s",
                (session.session_id, entered.target_id),
            ).fetchone()
        assert workflow == {"workflow_revision": 2}
        resolution = await session.resolve_plan_question(
            command_id="command:round9-2-answer",
            workflow_id=entered.target_id,
            expected_workflow_revision=2,
            interaction_id=opened.interaction_id,
            answer=PlanQuestionAnswer(PlanQuestionAnswerKind.OPTION, option_ordinal=0),
        )
        assert resolution.tool_result_settlement is not None
        assert resolution.tool_result_settlement.tool_call_id == "call:plan-question"
        result = await asyncio.wait_for(running, timeout=5)
        assert result.final_text == "PLAN_QUESTION_COMPLETE"
        assert not (workspace / "plan-sibling-must-not-exist.txt").exists()
        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            attempts = connection.execute(
                "SELECT count(*) AS count FROM pulsara_v3.tool_execution_attempts "
                "WHERE session_id = %s",
                (session.session_id,),
            ).fetchone()
            results = connection.execute(
                "SELECT result.tool_call_id, result.result_origin_kind, "
                "result.result_state FROM pulsara_v3.tool_results AS result "
                "JOIN pulsara_v3.transcript_entries AS entry "
                "ON entry.session_id = result.session_id "
                "AND entry.id = result.result_entry_id "
                "WHERE result.session_id = %s ORDER BY entry.entry_sequence",
                (session.session_id,),
            ).fetchall()
        assert attempts == {"count": 0}
        assert [value["tool_call_id"] for value in results] == [
            "call:barrier-sibling",
            "call:plan-question",
        ]
        await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()

    asyncio.run(scenario())
    logs = _log_values(log_path)
    tool_logs = [
        value["stdin"]
        for value in logs
        if value["stdin"]["hook_event_name"] in {"PreToolUse", "PostToolUse"}  # type: ignore[index]
    ]
    assert [value["hook_event_name"] for value in tool_logs] == [
        "PreToolUse",
        "PostToolUse",
        "PostToolUse",
    ]
    assert [value.get("tool_use_id") for value in tool_logs] == [
        "call:plan-question",
        "call:barrier-sibling",
        "call:plan-question",
    ]
    assert isinstance(tool_logs[2]["tool_response"], dict)
    assert logs[-1]["stdin"]["hook_event_name"] == "SessionEnd"  # type: ignore[index]


def test_round9_2_subagent_start_stop_real_owner_and_one_shot_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    driver = tmp_path / "hook_driver.py"
    log_path = tmp_path / "hooks.jsonl"
    driver.write_text(_HOOK_DRIVER, encoding="utf-8")
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        user_config=_config(
            _command(driver, log_path, "USER"),
            ("SubagentStart", "SubagentStop", "SessionEnd"),
        ),
        workspace_config=None,
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    model = _SubagentHookModel(log_path)
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=_runtime(stage2_migrated_postgres_database.runtime_dsn)
        )
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
        )
        await session.update_model_call_binding(
            test_model_binding(core._model_runtime)  # noqa: SLF001
        )
        try:
            result = await session.run_turn(
                "Delegate one child lifecycle probe.",
                command_id="command:round9-2-subagent",
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )
            assert result.final_text == "ROOT_AFTER_CHILD_COMPLETE"
            deadline = monotonic() + 5
            rows = ()
            while True:
                rows = session.repository.list_subagent_tasks(
                    session_id=session.session_id,
                    maximum_items=50,
                    deadline_monotonic=monotonic() + 5,
                )
                if len(rows) == 1 and rows[0]["status"] == "COMPLETED":
                    break
                assert monotonic() < deadline
                await asyncio.sleep(0.01)
            child_requests = [
                request
                for request in model.requests
                if request.compiled_input.canonical_input_identity.conversation_scope_kind
                is ModelInputScopeKind.SUBAGENT_TASK
            ]
            assert len(child_requests) == 2
            first_context = _hook_observations(child_requests[0])
            assert any(
                "SUBAGENT_START_CONTEXT:USER" in item.body for item in first_context
            )
            second_context = _hook_observations(child_requests[1])
            assert any(
                "SUBAGENT_STOP_CONTINUE_ONCE" in item.body for item in second_context
            )
        finally:
            await core.close_session(session.host_session_id, close_conversation=True)
            await core.shutdown()

    asyncio.run(scenario())
    logs = _log_values(log_path)
    lifecycle = [
        value["stdin"]
        for value in logs
        if value["stdin"]["hook_event_name"]  # type: ignore[index]
        in {"SubagentStart", "SubagentStop"}
    ]
    assert [value["hook_event_name"] for value in lifecycle] == [
        "SubagentStart",
        "SubagentStop",
        "SubagentStop",
    ]
    assert lifecycle[0]["turn_id"] == lifecycle[1]["turn_id"]
    assert lifecycle[0]["agent_id"] == lifecycle[1]["agent_id"]
    assert lifecycle[0]["agent_type"] == "general_worker"
    assert lifecycle[0]["permission_mode"] == "bypassPermissions"
    assert [value["stop_hook_active"] for value in lifecycle[1:]] == [
        False,
        True,
    ]
    assert logs[-1]["stdin"]["hook_event_name"] == "SessionEnd"  # type: ignore[index]


def test_round9_2_idle_compaction_runs_real_pre_and_post_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    driver = tmp_path / "hook_driver.py"
    log_path = tmp_path / "hooks.jsonl"
    driver.write_text(_HOOK_DRIVER, encoding="utf-8")
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        user_config=_config(
            _command(driver, log_path, "USER"),
            ("SessionStart", "PreCompact", "PostCompact", "SessionEnd"),
        ),
        workspace_config=None,
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    model = _CompactionScriptedModel(
        [
            _text_stream("FIRST_HISTORY_RESPONSE", "text:history:1"),
            _text_stream("SECOND_HISTORY_RESPONSE", "text:history:2"),
            _text_stream("THIRD_HISTORY_RESPONSE", "text:history:3"),
            _text_stream("AFTER_IDLE_COMPACTION", "text:history:4"),
        ],
        "Earlier work completed; retain the current objective and exact next step.",
    )
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=_runtime(stage2_migrated_postgres_database.runtime_dsn)
        )
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
        )
        await session.update_model_call_binding(
            test_model_binding(core._model_runtime)  # noqa: SLF001
        )
        for index in range(3):
            result = await session.run_turn(
                f"history request {index}: " + ("context " * 4096),
                command_id=f"command:round9-2-history:{index}",
                requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
            assert result.final_text.endswith("HISTORY_RESPONSE")
        outcome = await session.compact_context(
            command_id="command:round9-2-idle-compact",
            force=True,
        )
        assert outcome.disposition is CompactionDisposition.COMPACTED
        assert outcome.snapshot_id is not None
        assert model.summary_transport.open_count == 1
        with session.repository.connection_provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            row_factory=dict_row,
            deadline_monotonic=monotonic() + 5,
        ) as connection:
            snapshots = connection.execute(
                "SELECT id, source_through_sequence FROM pulsara_v3.context_snapshots "
                "WHERE session_id = %s",
                (session.session_id,),
            ).fetchall()
        assert len(snapshots) == 1
        assert snapshots[0]["id"] == outcome.snapshot_id
        assert snapshots[0]["source_through_sequence"] > 0
        after = await session.run_turn(
            "continue after idle compaction",
            command_id="command:round9-2-after-idle-compact",
            requested_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        assert after.final_text == "AFTER_IDLE_COMPACTION"
        await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()

    asyncio.run(scenario())
    logs = _log_values(log_path)
    compact = [
        value["stdin"]
        for value in logs
        if value["stdin"]["hook_event_name"] in {"PreCompact", "PostCompact"}  # type: ignore[index]
    ]
    assert [value["hook_event_name"] for value in compact] == [
        "PreCompact",
        "PostCompact",
    ]
    assert all(value["trigger"] == "manual" for value in compact)
    assert all("permission_mode" not in value for value in compact)
    session_starts = [
        value["stdin"]
        for value in logs
        if value["stdin"]["hook_event_name"] == "SessionStart"  # type: ignore[index]
    ]
    assert [value["source"] for value in session_starts] == ["startup", "compact"]
    assert logs[-1]["stdin"]["hook_event_name"] == "SessionEnd"  # type: ignore[index]


def test_round9_2_compact_before_resumed_first_open_supersedes_resume_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "resume-input.txt").write_text("resume input", encoding="utf-8")
    driver = tmp_path / "hook_driver.py"
    log_path = tmp_path / "hooks.jsonl"
    driver.write_text(_HOOK_DRIVER, encoding="utf-8")
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        user_config=_config(
            _command(driver, log_path, "USER"),
            ("SessionStart", "PreCompact", "PostCompact", "SessionEnd"),
        ),
        workspace_config=None,
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    calls = [
        _text_stream(f"RESUME_HISTORY_{index}", f"text:resume-history:{index}")
        for index in range(6)
    ]
    calls.extend(
        (
            _tool_stream(
                "read_file",
                "call:resume-after-compact",
                {"path": "resume-input.txt"},
            ),
            _text_stream("RESUME_COMPACTION_COMPLETE", "text:resume-final"),
        )
    )
    model = _CompactionScriptedModel(
        calls,
        "Earlier resumed history is complete; preserve the current request.",
    )
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=_runtime(stage2_migrated_postgres_database.runtime_dsn)
        )
        first = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
        )
        await first.update_model_call_binding(
            test_model_binding(core._model_runtime)  # noqa: SLF001
        )
        session_id = first.session_id
        for index in range(6):
            result = await first.run_turn(
                f"resume history {index}: " + ("context " * 4096),
                command_id=f"command:round9-2-resume-history:{index}",
                requested_permission_mode=PermissionMode.ACCEPT_EDITS,
            )
            assert result.final_text == f"RESUME_HISTORY_{index}"
        prior = model.requests[-1]
        prior_tokens = (
            prior.wire_input_plan.quote.final_wire_estimated_input_tokens
        )
        budget = prior.prepared_call.compile_binding.effective_input_budget_tokens
        prior_ratio = prior_tokens / budget
        trigger_ratio = min(0.8, prior_ratio + 0.005)
        target_ratio = max(0.001, trigger_ratio - 0.001)
        await core.close_session(first.host_session_id, close_conversation=False)

        resumed = await core.resume_session(
            session_id,
            workspace_input=HostWorkspaceInput(
                workspace_kind="project", workspace_root=workspace
            ),
        )
        resumed._compaction.policy = replace(  # noqa: SLF001
            resumed._compaction.policy,  # noqa: SLF001
            auto_trigger_ratio=trigger_ratio,
            post_compaction_target_ratio=target_ratio,
            minimum_reclaim_tokens=1,
        )
        result = await resumed.run_turn(
            "resume first physical attempt: " + ("context " * 4096),
            command_id="command:round9-2-resume-compaction",
            requested_permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        assert result.final_text == "RESUME_COMPACTION_COMPLETE"
        # The trigger and target are derived from the installed final-wire
        # quote. The first exact-fit successor is adopted without retrying a
        # changed semantic prefix under a stale semantic-token threshold.
        assert model.summary_transport.open_count == 1
        assert len(model.requests) == 8
        await core.close_session(resumed.host_session_id, close_conversation=True)
        await core.shutdown()

    asyncio.run(scenario())
    logs = _log_values(log_path)
    starts = [
        value["stdin"]
        for value in logs
        if value["stdin"]["hook_event_name"] == "SessionStart"  # type: ignore[index]
    ]
    assert [value["source"] for value in starts] == ["startup", "compact"]
    compact_lifecycle = [
        value["stdin"]["hook_event_name"]
        for value in logs
        if value["stdin"]["hook_event_name"]  # type: ignore[index]
        in {"PreCompact", "PostCompact", "SessionStart"}
    ]
    assert compact_lifecycle[-3:] == ["PreCompact", "PostCompact", "SessionStart"]


def test_round9_2_active_compaction_runs_post_then_root_compact_session_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "active-input.txt").write_text("active input", encoding="utf-8")
    driver = tmp_path / "hook_driver.py"
    log_path = tmp_path / "hooks.jsonl"
    driver.write_text(_HOOK_DRIVER, encoding="utf-8")
    _install_trusted_sources(
        home=home,
        workspace_root=workspace,
        user_config=_config(
            _command(driver, log_path, "USER"),
            ("SessionStart", "PreCompact", "PostCompact", "SessionEnd"),
        ),
        workspace_config=None,
    )
    monkeypatch.setenv("PULSARA_HOME", str(home))
    model = _ActiveCompactionModel()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=_runtime(stage2_migrated_postgres_database.runtime_dsn)
        )
        session = None
        running = None
        compacting = None
        try:
            session = await core.open_session(
                HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
            )
            await session.update_model_call_binding(
                test_model_binding(core._model_runtime)  # noqa: SLF001
            )
            for index in range(3):
                await session.run_turn(
                    f"active history {index}: " + ("context " * 4096),
                    command_id=f"command:round9-2-active-history:{index}",
                    requested_permission_mode=PermissionMode.ACCEPT_EDITS,
                )
            running = asyncio.create_task(
                session.run_turn(
                    "active compaction request: " + ("context " * 4096),
                    command_id="command:round9-2-active-turn",
                    requested_permission_mode=PermissionMode.ACCEPT_EDITS,
                )
            )
            await asyncio.wait_for(model.active_provider_started.wait(), timeout=5)
            active_turn_id = session._active_turn_id  # noqa: SLF001
            assert active_turn_id is not None
            compacting = asyncio.create_task(
                session.compact_context(
                    command_id="command:round9-2-active-compact",
                    force=True,
                    expected_active_turn_id=active_turn_id,
                )
            )
            deadline = monotonic() + 5
            while (
                await session._compaction.find_manual(  # noqa: SLF001
                    command_id="command:round9-2-active-compact",
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                )
                is None
            ):
                assert monotonic() < deadline
                await asyncio.sleep(0.01)
            model.active_provider_release.set()
            outcome = await asyncio.wait_for(compacting, timeout=10)
            assert outcome.disposition is CompactionDisposition.COMPACTED
            result = await asyncio.wait_for(running, timeout=10)
            assert result.final_text == "ACTIVE_COMPACTION_COMPLETE"
            assert len(model.requests) == 5
            assert model.summary_transport.open_count == 1
            final_observations = _hook_observations(model.requests[-1])
            assert any(
                "SESSION_START_CONTEXT:USER:compact" in item.body
                for item in final_observations
            )
        finally:
            model.active_provider_release.set()
            if session is not None:
                await core.close_session(
                    session.host_session_id, close_conversation=True
                )
            for task in (running, compacting):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (running, compacting) if task is not None),
                return_exceptions=True,
            )
            await core.shutdown()

    asyncio.run(scenario())
    logs = _log_values(log_path)
    lifecycle = [
        value["stdin"]
        for value in logs
        if value["stdin"]["hook_event_name"]  # type: ignore[index]
        in {"SessionStart", "PreCompact", "PostCompact"}
    ]
    assert [value["hook_event_name"] for value in lifecycle] == [
        "SessionStart",
        "PreCompact",
        "PostCompact",
        "SessionStart",
    ]
    assert [
        value.get("source")
        for value in lifecycle
        if value["hook_event_name"] == "SessionStart"
    ] == ["startup", "compact"]
    assert lifecycle[1]["trigger"] == "manual"
    assert lifecycle[2]["trigger"] == "manual"
    assert logs[-1]["stdin"]["hook_event_name"] == "SessionEnd"  # type: ignore[index]
