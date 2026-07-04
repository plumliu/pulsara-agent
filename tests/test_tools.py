import asyncio
import json
import tempfile
import threading
import time

import pytest
from tests.support.runtime_session import in_memory_runtime_session

from pulsara_agent.event import EventContext, TerminalProcessCompletedEvent, ToolResultEndEvent, ToolResultTextDeltaEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.message import ToolResultBlock, ToolResultState
from pulsara_agent.runtime import RuntimeSession
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    PermissionState,
    TerminalAccess,
)
from pulsara_agent.runtime.tool_artifacts import (
    InMemoryToolResultArtifactIndex,
    ToolResultArtifactOptions,
    ToolResultArtifactRecord,
    ToolResultArtifactService,
)
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.tools import (
    ToolCall,
    ToolExecutionResult,
    ToolExecutor,
    ToolResultArtifactCandidate,
    ToolRuntimeContext,
    build_core_tool_registry,
)
from pulsara_agent.tools.builtins.terminal import TerminalTool
from pulsara_agent.tools.builtins.terminal_process import TerminalProcessTool
from pulsara_agent.tools.builtins.todo import TodoTool
from pulsara_agent.tools.registry import ToolRegistry


CTX = EventContext(run_id="run:tools", turn_id="turn:tools", reply_id="reply:tools")


class _AsyncContextProbeTool:
    name = "async_context_probe"
    description = "Capture async tool runtime context for a dispatch regression test."
    parameters = {"type": "object", "properties": {}}
    is_read_only = True
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.loop = None
        self.runtime_context: ToolRuntimeContext | None = None

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        self.loop = asyncio.get_running_loop()
        self.runtime_context = runtime_context
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            status=ToolResultState.SUCCESS,
            output="ok",
        )


def make_registry(tmp_path):
    return build_core_tool_registry(in_memory_runtime_session(tmp_path))


def make_runtime_executor(tmp_path) -> tuple[RuntimeSession, ToolExecutor]:
    runtime_session = in_memory_runtime_session(tmp_path)
    return runtime_session, runtime_session.create_tool_executor(
        record_event=runtime_session.make_thread_recorder()
    )


def execute_tool(tmp_path, name: str, arguments: dict) -> tuple[ToolExecutor, object]:
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)
    result = executor.execute(
        ToolCall(id=f"call:{name}", name=name, arguments=arguments),
        event_context=CTX,
    )
    return executor, result


def test_async_tool_runs_on_calling_loop_with_runtime_context() -> None:
    probe = _AsyncContextProbeTool()
    registry = ToolRegistry()
    registry.register(probe)
    executor = ToolExecutor(registry=registry, runtime_session_id="runtime:async-probe")

    async def run_probe() -> tuple[object, ToolExecutionResult]:
        loop = asyncio.get_running_loop()
        result = await executor.execute_async(
            ToolCall(id="call:async-probe", name=probe.name),
            event_context=CTX,
        )
        return loop, result

    calling_loop, result = asyncio.run(run_probe())

    assert result.status is ToolResultState.SUCCESS
    assert probe.loop is calling_loop
    assert probe.runtime_context == ToolRuntimeContext(
        runtime_session_id="runtime:async-probe",
        event_context=CTX,
    )


def test_core_tool_registry_exposes_minimal_builtin_tools(tmp_path) -> None:
    registry = make_registry(tmp_path)

    assert registry.names() == [
        "artifact_read",
        "ask_plan_question",
        "edit_file",
        "enter_plan",
        "exit_plan",
        "read_file",
        "search_files",
        "terminal",
        "terminal_process",
        "todo",
        "write_file",
    ]
    assert [spec.name for spec in registry.tool_specs()] == registry.names()
    assert all(spec.parameters["type"] == "object" for spec in registry.tool_specs())
    assert not any(name.startswith("remember_") for name in registry.names())
    assert "propose_memory" not in registry.names()


def test_core_tool_registry_can_enable_memory_write_tools(tmp_path) -> None:
    registry = build_core_tool_registry(
        in_memory_runtime_session(tmp_path),
        memory_proposal_sink=MemoryProposalSink(),
    )

    assert "propose_memory" not in registry.names()
    assert {
        "remember_action_boundary",
        "remember_claim",
        "remember_decision",
        "remember_observation",
        "remember_preference",
    }.issubset(registry.names())


def test_core_tool_registry_registers_all_tools_under_read_only(tmp_path) -> None:
    # PERMISSION_POLICY_CONTRACT: gate is the sole authority. Tools stay fully
    # registered (visible) under read-only; the gate denies disallowed calls.
    registry = build_core_tool_registry(
        in_memory_runtime_session(tmp_path),
        permission_state=PermissionState.from_policy(
            EffectivePermissionPolicy(
                profile=PermissionProfile.READ_ONLY,
                approval=ApprovalPolicy.ON_REQUEST,
                terminal=TerminalAccess.OFF,
            )
        ),
    )

    names = registry.names()
    assert {"artifact_read", "read_file", "search_files", "todo"}.issubset(names)
    # Mutating tools remain VISIBLE under read-only (blocked later by the gate).
    assert {"edit_file", "write_file", "terminal", "terminal_process"}.issubset(names)
    # Workflow tools are also always visible; runtime handles them before the permission gate.
    assert {"enter_plan", "ask_plan_question", "exit_plan"}.issubset(names)


def test_core_tool_registry_keeps_terminal_tools_registered_when_terminal_off(tmp_path) -> None:
    registry = build_core_tool_registry(
        in_memory_runtime_session(tmp_path),
        permission_state=PermissionState.from_policy(
            EffectivePermissionPolicy(
                profile=PermissionProfile.WORKSPACE_GUARDED,
                approval=ApprovalPolicy.RISKY_ONLY,
                terminal=TerminalAccess.OFF,
            )
        ),
    )

    # terminal/terminal_process are visible even with terminal=off; the gate
    # denies them at call time rather than hiding them from the registry.
    assert {"edit_file", "write_file", "terminal", "terminal_process"}.issubset(registry.names())


def test_core_tool_registry_is_constant_across_permission_modes(tmp_path) -> None:
    # The tools array must be identical across modes so the prompt prefix cache
    # stays stable when the user switches mode mid-conversation.
    def names_for(profile, approval, terminal):
        return set(
            build_core_tool_registry(
                in_memory_runtime_session(tmp_path),
                permission_state=PermissionState.from_policy(
                    EffectivePermissionPolicy(profile=profile, approval=approval, terminal=terminal)
                ),
            ).names()
        )

    read_only = names_for(PermissionProfile.READ_ONLY, ApprovalPolicy.ON_REQUEST, TerminalAccess.OFF)
    bypass = names_for(PermissionProfile.TRUSTED_HOST, ApprovalPolicy.NEVER, TerminalAccess.ALLOW)
    ask = names_for(PermissionProfile.TRUSTED_HOST, ApprovalPolicy.ON_REQUEST, TerminalAccess.ASK)

    assert read_only == bypass == ask


def test_terminal_tool_schema_uses_yield_model_hard_cut(tmp_path) -> None:
    registry = make_registry(tmp_path)
    terminal = next(spec for spec in registry.tool_specs() if spec.name == "terminal")
    properties = terminal.parameters["properties"]

    assert terminal.parameters["additionalProperties"] is False
    assert "yield_time_ms" in properties
    assert "tty" in properties
    assert "background" not in properties
    assert "timeout_seconds" not in properties
    assert "session_id" not in properties
    assert "max_lifetime_seconds" not in properties


def test_read_file_reads_workspace_and_host_local_text_files(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello Pulsara\nsecond line\n", encoding="utf-8")

    _, result = execute_tool(tmp_path, "read_file", {"path": "note.txt", "offset": 2, "limit": 1})

    assert result.status is ToolResultState.SUCCESS
    assert result.metadata["access_scope"] == "workspace"
    assert result.metadata["workspace_relative"] is True
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["path"] == "note.txt"
    assert payload["access_scope"] == "workspace"
    assert payload["workspace_relative"] is True
    assert payload["offset"] == 2
    assert payload["total_lines"] == 2
    assert payload["content"] == "2|second line"

    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    _, outside_result = execute_tool(tmp_path, "read_file", {"path": str(outside)})

    assert outside_result.status is ToolResultState.SUCCESS
    assert outside_result.metadata["workspace_relative"] is False
    outside_payload = json.loads(outside_result.output)
    assert outside_payload["status"] == "ok"
    assert outside_payload["path"] == str(outside.resolve())
    assert outside_payload["access_scope"] in {"home", "temp", "external_absolute"}
    assert outside_payload["workspace_relative"] is False
    assert outside_payload["content"] == "1|secret"

    _, relative_escape = execute_tool(tmp_path, "read_file", {"path": "../outside.txt"})
    assert relative_escape.status is ToolResultState.ERROR
    assert "escapes workspace root" in relative_escape.output


def test_read_file_allows_explicit_host_local_sensitive_text_path_by_design(tmp_path) -> None:
    env_file = tmp_path.parent / f"{tmp_path.name}.env"
    env_file.write_text("TOKEN=plain-text-secret\n", encoding="utf-8")

    _, result = execute_tool(tmp_path, "read_file", {"path": str(env_file)})

    assert result.status is ToolResultState.SUCCESS
    assert result.metadata["workspace_relative"] is False
    payload = json.loads(result.output)
    assert payload["workspace_relative"] is False
    assert payload["content"] == "1|TOKEN=plain-text-secret"


def test_write_and_edit_file_still_block_path_escape(tmp_path) -> None:
    outside = tmp_path.parent / "outside-write.txt"
    outside.write_text("before", encoding="utf-8")

    _, write_result = execute_tool(tmp_path, "write_file", {"path": str(outside), "content": "after"})
    _, edit_result = execute_tool(
        tmp_path,
        "edit_file",
        {"path": str(outside), "old_text": "before", "new_text": "after"},
    )

    assert write_result.status is ToolResultState.ERROR
    assert "escapes workspace root" in write_result.output
    assert edit_result.status is ToolResultState.ERROR
    assert "escapes workspace root" in edit_result.output
    assert outside.read_text(encoding="utf-8") == "before"


def test_read_file_deduplicates_unchanged_repeated_reads(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello Pulsara\n", encoding="utf-8")

    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)
    call = ToolCall(id="call:read", name="read_file", arguments={"path": "note.txt"})

    first = executor.execute(call, event_context=CTX)
    second = executor.execute(call, event_context=CTX)
    third = executor.execute(call, event_context=CTX)

    assert json.loads(first.output)["content"] == "1|hello Pulsara"
    assert json.loads(second.output)["status"] == "unchanged"
    assert third.status is ToolResultState.ERROR
    assert "Repeated read blocked" in json.loads(third.output)["error"]


def test_search_files_finds_matching_text(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "memory.md").write_text("JSON-LD memory\n", encoding="utf-8")

    _, result = execute_tool(
        tmp_path,
        "search_files",
        {"pattern": "JSON-LD", "path": "src", "limit": 5},
    )

    assert result.status is ToolResultState.SUCCESS
    assert result.metadata["workspace_relative"] is True
    payload = json.loads(result.output)
    assert payload["total_count"] == 1
    assert payload["access_scope"] == "workspace"
    assert payload["workspace_relative"] is True
    assert payload["matches"][0]["path"].endswith("memory.md")
    assert payload["matches"][0]["content"] == "JSON-LD memory"


def test_search_files_can_search_specific_host_local_directory_and_blocks_broad_roots(tmp_path) -> None:
    external_dir = tmp_path.parent / f"{tmp_path.name}-external-search"
    external_dir.mkdir()
    (external_dir / "skill.md").write_text("HOST_LOCAL_SEARCH_SENTINEL\n", encoding="utf-8")

    _, result = execute_tool(
        tmp_path,
        "search_files",
        {"pattern": "HOST_LOCAL_SEARCH_SENTINEL", "path": str(external_dir), "limit": 5},
    )

    assert result.status is ToolResultState.SUCCESS
    assert result.metadata["workspace_relative"] is False
    payload = json.loads(result.output)
    assert payload["total_count"] == 1
    assert payload["access_scope"] in {"home", "temp", "external_absolute"}
    assert payload["workspace_relative"] is False
    assert payload["matches"][0]["path"] == str((external_dir / "skill.md").resolve())

    _, broad = execute_tool(
        tmp_path,
        "search_files",
        {"pattern": "SHOULD_NOT_SCAN_TEMP_ROOT", "path": tempfile.gettempdir(), "limit": 1},
    )

    assert broad.status is ToolResultState.ERROR
    assert "refusing broad recursive search root outside workspace" in broad.output

    _, relative_escape = execute_tool(
        tmp_path,
        "search_files",
        {"pattern": "HOST_LOCAL_SEARCH_SENTINEL", "path": "..", "limit": 1},
    )
    assert relative_escape.status is ToolResultState.ERROR
    assert "escapes workspace root" in relative_escape.output


def test_search_files_can_find_files_by_name(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "memory_design.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "src" / "other.py").write_text("x\n", encoding="utf-8")

    _, result = execute_tool(
        tmp_path,
        "search_files",
        {"pattern": "memory", "target": "files", "path": "src"},
    )

    payload = json.loads(result.output)
    assert payload["target"] == "files"
    assert payload["files"] == ["src/memory_design.md"]


def test_write_file_creates_and_overwrites_file_atomically(tmp_path) -> None:
    _, created = execute_tool(
        tmp_path,
        "write_file",
        {"path": "docs/design.md", "content": "v1"},
    )

    assert created.status is ToolResultState.SUCCESS
    assert (tmp_path / "docs" / "design.md").read_text(encoding="utf-8") == "v1"

    _, overwritten = execute_tool(
        tmp_path,
        "write_file",
        {"path": "docs/design.md", "content": "v2"},
    )
    assert overwritten.status is ToolResultState.SUCCESS
    payload = json.loads(overwritten.output)
    assert payload["status"] == "ok"
    assert payload["files_modified"] == ["docs/design.md"]
    assert (tmp_path / "docs" / "design.md").read_text(encoding="utf-8") == "v2"


def test_write_file_warns_when_file_changed_after_read(tmp_path) -> None:
    target = tmp_path / "docs" / "design.md"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    executor.execute(
        ToolCall(id="call:read", name="read_file", arguments={"path": "docs/design.md"}),
        event_context=CTX,
    )
    target.write_text("external", encoding="utf-8")
    result = executor.execute(
        ToolCall(
            id="call:write",
            name="write_file",
            arguments={"path": "docs/design.md", "content": "v2"},
        ),
        event_context=CTX,
    )

    payload = json.loads(result.output)
    assert result.status is ToolResultState.SUCCESS
    assert "modified since the last read_file call" in payload["_warning"]


def test_edit_file_replaces_exact_text_and_rejects_ambiguous_match(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("name = 'old'\nname = 'old'\n", encoding="utf-8")

    _, ambiguous = execute_tool(
        tmp_path,
        "edit_file",
        {"path": "app.py", "old_text": "old", "new_text": "new"},
    )
    assert ambiguous.status is ToolResultState.ERROR
    assert "Found 2 matches" in json.loads(ambiguous.output)["error"]

    _, edited = execute_tool(
        tmp_path,
        "edit_file",
        {"path": "app.py", "old_text": "old", "new_text": "new", "replace_all": True},
    )
    assert edited.status is ToolResultState.SUCCESS
    payload = json.loads(edited.output)
    assert payload["strategy"] == "exact"
    assert "-name = 'old'" in payload["diff"]
    assert "+name = 'new'" in payload["diff"]
    assert target.read_text(encoding="utf-8") == "name = 'new'\nname = 'new'\n"


def test_edit_file_uses_fuzzy_whitespace_matching(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    _, result = execute_tool(
        tmp_path,
        "edit_file",
        {
            "path": "app.py",
            "old_text": "def hello(): return 'world'",
            "new_text": "def hello():\n    return 'Pulsara'",
        },
    )

    payload = json.loads(result.output)
    assert result.status is ToolResultState.SUCCESS
    assert payload["strategy"] == "whitespace_normalized"
    assert target.read_text(encoding="utf-8") == "def hello():\n    return 'Pulsara'\n"


def test_terminal_runs_command_in_workspace(tmp_path) -> None:
    _, result = execute_tool(tmp_path, "terminal", {"command": "pwd && printf hi"})
    payload = json.loads(result.output)

    assert result.status is ToolResultState.SUCCESS
    assert payload["status"] == "success"
    assert str(tmp_path) in payload["output"]
    assert payload["output"].endswith("hi")
    assert payload["cwd"] == str(tmp_path)


def test_terminal_tool_exposes_workdir_and_structured_json(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    registry = make_registry(tmp_path)
    terminal_spec = next(spec for spec in registry.tool_specs() if spec.name == "terminal")
    executor = ToolExecutor(registry=registry)

    result = executor.execute(
        ToolCall(id="call:terminal", name="terminal", arguments={"command": "pwd", "workdir": "src"}),
        event_context=CTX,
    )
    payload = json.loads(result.output)

    assert "workdir" in terminal_spec.parameters["properties"]
    assert "terminal_session_id" in terminal_spec.parameters["properties"]
    assert result.status is ToolResultState.SUCCESS
    assert payload["output"] == str(tmp_path / "src")
    assert payload["terminal_session_id"] == "default"
    assert payload["backend_type"] == "local"
    assert result.metadata["cwd"] == str(tmp_path / "src")
    assert result.metadata["terminal_session_id"] == "default"
    assert result.metadata["backend_type"] == "local"


def test_terminal_tool_payload_exposes_safe_env_diagnostics_only(tmp_path) -> None:
    _, result = execute_tool(tmp_path, "terminal", {"command": "printf hi"})
    payload = json.loads(result.output)

    assert result.status is ToolResultState.SUCCESS
    assert payload["env"]["shell_snapshot_used"] in {True, False}
    assert "sanitized_env_removed_count" in payload["env"]
    assert "path_entries_count" in payload["env"]
    assert "PATH" not in payload["env"]
    assert "HOME" not in payload["env"]
    assert result.metadata["env"] == payload["env"]


def test_terminal_process_tool_uses_shared_process_registry(tmp_path) -> None:
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    start = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "sleep 5", "yield_time_ms": 0},
        ),
        event_context=CTX,
    )
    start_payload = json.loads(start.output)
    process_id = start_payload["process_id"]

    poll = executor.execute(
        ToolCall(
            id="call:poll",
            name="terminal_process",
            arguments={"action": "poll", "process_id": process_id},
        ),
        event_context=CTX,
    )
    kill = executor.execute(
        ToolCall(
            id="call:kill",
            name="terminal_process",
            arguments={"action": "kill", "process_id": process_id},
        ),
        event_context=CTX,
    )

    assert start.status is ToolResultState.SUCCESS
    assert start_payload["status"] == "running"
    assert process_id
    assert json.loads(poll.output)["status"] == "running"
    assert json.loads(kill.output)["status"] == "killed"


def test_terminal_process_tool_lists_and_logs_retained_processes(tmp_path) -> None:
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    start = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "sleep 0.05 && printf LIST_LOG_OK", "yield_time_ms": 0},
        ),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]
    executor.execute(
        ToolCall(
            id="call:wait",
            name="terminal_process",
            arguments={"action": "wait", "process_id": process_id, "timeout_seconds": 2},
        ),
        event_context=CTX,
    )

    listed = executor.execute(
        ToolCall(id="call:list", name="terminal_process", arguments={"action": "list"}),
        event_context=CTX,
    )
    logged = executor.execute(
        ToolCall(
            id="call:log",
            name="terminal_process",
            arguments={"action": "log", "process_id": process_id},
        ),
        event_context=CTX,
    )
    list_payload = json.loads(listed.output)
    log_payload = json.loads(logged.output)

    assert listed.status is ToolResultState.SUCCESS
    assert list_payload["terminal_process_action"] == "list"
    assert list_payload["finished_process_count"] == 1
    assert list_payload["processes"][0]["process_id"] == process_id
    assert "started_at_monotonic" not in list_payload["processes"][0]
    assert "ended_at_monotonic" not in list_payload["processes"][0]
    assert log_payload["terminal_process_action"] == "log"
    assert log_payload["process_id"] == process_id
    assert log_payload["output"] == "LIST_LOG_OK"
    assert log_payload["process"]["duration_seconds"] >= 0


def test_terminal_process_wait_without_timeout_uses_finite_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pulsara_agent.tools.builtins.terminal_process.DEFAULT_WAIT_TIMEOUT_SECONDS",
        1,
    )
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    start = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "sleep 5", "yield_time_ms": 0},
        ),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]
    wait = executor.execute(
        ToolCall(
            id="call:wait",
            name="terminal_process",
            arguments={"action": "wait", "process_id": process_id},
        ),
        event_context=CTX,
    )
    kill = executor.execute(
        ToolCall(
            id="call:kill",
            name="terminal_process",
            arguments={"action": "kill", "process_id": process_id},
        ),
        event_context=CTX,
    )

    assert json.loads(wait.output)["status"] == "running"
    assert json.loads(kill.output)["status"] == "killed"


def test_terminal_process_wait_zero_timeout_uses_finite_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "pulsara_agent.tools.builtins.terminal_process.DEFAULT_WAIT_TIMEOUT_SECONDS",
        1,
    )
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    start = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "sleep 0.05 && printf done", "yield_time_ms": 0},
        ),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]
    wait = executor.execute(
        ToolCall(
            id="call:wait",
            name="terminal_process",
            arguments={"action": "wait", "process_id": process_id, "timeout_seconds": 0},
        ),
        event_context=CTX,
    )
    payload = json.loads(wait.output)

    assert wait.status is ToolResultState.SUCCESS
    assert payload["status"] == "success"
    assert payload["output"] == "done"


def test_terminal_removed_background_arguments_are_hard_cut(tmp_path) -> None:
    _, result = execute_tool(
        tmp_path,
        "terminal",
        {"command": "sleep 5", "background": True, "timeout_seconds": 1},
    )
    payload = json.loads(result.output)

    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert "no longer supported" in payload["error"]
    assert "background" in payload["error"]
    assert "timeout_seconds" in payload["error"]


def test_terminal_long_running_command_yields_to_process_id(tmp_path) -> None:
    _, result = execute_tool(tmp_path, "terminal", {"command": "sleep 0.2 && printf done", "yield_time_ms": 10})
    payload = json.loads(result.output)

    assert result.status is ToolResultState.SUCCESS
    assert payload["status"] == "running"
    assert payload["process_id"]
    assert payload["yielded_to_background"] is True


def test_terminal_max_lifetime_argument_is_not_model_facing(tmp_path) -> None:
    _, result = execute_tool(
        tmp_path,
        "terminal",
        {"command": "sleep 1", "yield_time_ms": 0, "max_lifetime_seconds": 1},
    )
    payload = json.loads(result.output)

    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert "max_lifetime_seconds" in payload["error"]
    assert "runtime-only" in payload["error"]
    assert "no longer supported" not in payload["error"]


def test_terminal_max_output_chars_zero_falls_back_to_default(tmp_path) -> None:
    _, result = execute_tool(
        tmp_path,
        "terminal",
        {
            "command": "python -c 'print(\"x\" * 400)'",
            "max_output_chars": 0,
        },
    )
    payload = json.loads(result.output)

    assert result.status is ToolResultState.SUCCESS
    assert payload["status"] == "success"
    assert len(payload["output"]) == 400
    assert payload["truncated"] is False


def test_terminal_max_output_chars_tiny_value_is_floored(tmp_path) -> None:
    _, result = execute_tool(
        tmp_path,
        "terminal",
        {
            "command": "python -c 'print(\"y\" * 600)'",
            "max_output_chars": 5,
        },
    )
    payload = json.loads(result.output)

    assert result.status is ToolResultState.SUCCESS
    assert payload["status"] == "success"
    assert payload["output"].startswith("y" * 100)
    assert "OUTPUT TRUNCATED" in payload["output"]
    assert payload["truncated"] is True


def test_terminal_process_max_output_chars_tiny_value_is_floored(tmp_path) -> None:
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    start = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "python -c 'print(\"p\" * 600)'", "yield_time_ms": 0},
        ),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]
    wait = executor.execute(
        ToolCall(
            id="call:wait",
            name="terminal_process",
            arguments={
                "action": "wait",
                "process_id": process_id,
                "timeout_seconds": 2,
                "max_output_chars": 5,
            },
        ),
        event_context=CTX,
    )
    payload = json.loads(wait.output)

    assert wait.status is ToolResultState.SUCCESS
    assert payload["status"] == "success"
    assert payload["output"].startswith("p" * 100)
    assert "OUTPUT TRUNCATED" in payload["output"]
    assert payload["truncated"] is True


def test_terminal_shell_background_wrapper_returns_guidance(tmp_path) -> None:
    _, result = execute_tool(tmp_path, "terminal", {"command": "sleep 5 &"})
    payload = json.loads(result.output)

    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert payload["policy_code"] == "use_terminal_yield"
    assert payload["suggested_args"] == {"yield_time_ms": 0}
    assert "yield semantics" in payload["error"]


def test_terminal_notify_on_complete_is_hard_cut(tmp_path) -> None:
    _, result = execute_tool(
        tmp_path,
        "terminal",
        {"command": "sleep 1", "notify_on_complete": True},
    )
    payload = json.loads(result.output)

    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert "notify_on_complete" in payload["error"]


def test_terminal_hardline_command_called_directly_fails_closed(tmp_path) -> None:
    _, result = execute_tool(tmp_path, "terminal", {"command": "rm -rf /"})
    payload = json.loads(result.output)

    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert payload["policy_code"] == "hardline_terminal_command"
    assert "hardline permission policy" in payload["error"]


def test_terminal_process_tool_submit_and_close_stdin(tmp_path) -> None:
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    start = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={
                "command": "python -c 'import sys; data=sys.stdin.read(); print(\"GOT:\" + data)'",
                "yield_time_ms": 0,
            },
        ),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]

    submit = executor.execute(
        ToolCall(
            id="call:submit",
            name="terminal_process",
            arguments={"action": "submit", "process_id": process_id, "data": "hello"},
        ),
        event_context=CTX,
    )
    close = executor.execute(
        ToolCall(
            id="call:close",
            name="terminal_process",
            arguments={"action": "close_stdin", "process_id": process_id},
        ),
        event_context=CTX,
    )
    wait = executor.execute(
        ToolCall(
            id="call:wait",
            name="terminal_process",
            arguments={"action": "wait", "process_id": process_id, "timeout_seconds": 2},
        ),
        event_context=CTX,
    )

    submit_payload = json.loads(submit.output)
    close_payload = json.loads(close.output)
    wait_payload = json.loads(wait.output)
    assert submit.status is ToolResultState.SUCCESS
    assert submit_payload["terminal_process_action"] == "submit"
    assert close_payload["terminal_process_action"] == "close_stdin"
    assert wait_payload["status"] == "success"
    assert wait_payload["output"] == "GOT:hello"


def test_terminal_process_tool_rejects_write_after_finished_process(tmp_path) -> None:
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    start = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "sleep 0.05 && printf done", "yield_time_ms": 0},
        ),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]
    executor.execute(
        ToolCall(
            id="call:wait",
            name="terminal_process",
            arguments={"action": "wait", "process_id": process_id, "timeout_seconds": 2},
        ),
        event_context=CTX,
    )
    write = executor.execute(
        ToolCall(
            id="call:write",
            name="terminal_process",
            arguments={"action": "write", "process_id": process_id, "data": "late"},
        ),
        event_context=CTX,
    )
    payload = json.loads(write.output)

    assert write.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert "finished" in payload["error"]


def test_terminal_tool_fails_closed_when_policy_disables_terminal(tmp_path) -> None:
    tool = TerminalTool(
        tmp_path,
        permission_state=PermissionState.from_policy(
            EffectivePermissionPolicy(
                profile=PermissionProfile.WORKSPACE_GUARDED,
                approval=ApprovalPolicy.RISKY_ONLY,
                terminal=TerminalAccess.OFF,
            )
        ),
    )

    result = tool.execute(ToolCall(id="call:terminal", name="terminal", arguments={"command": "pwd"}))
    payload = json.loads(result.output)

    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert payload["policy_code"] == "terminal_access_off"


def test_terminal_process_tool_fails_closed_when_policy_disables_terminal(tmp_path) -> None:
    tool = TerminalProcessTool(
        tmp_path,
        permission_state=PermissionState.from_policy(
            EffectivePermissionPolicy(
                profile=PermissionProfile.WORKSPACE_GUARDED,
                approval=ApprovalPolicy.RISKY_ONLY,
                terminal=TerminalAccess.OFF,
            )
        ),
    )

    result = tool.execute(
        ToolCall(
            id="call:process",
            name="terminal_process",
            arguments={"action": "list"},
        )
    )
    payload = json.loads(result.output)

    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert payload["policy_code"] == "terminal_access_off"


def test_terminal_tool_context_records_yielded_completion_event(tmp_path) -> None:
    registry = make_registry(tmp_path)
    event_log = InMemoryEventLog()
    executor = ToolExecutor(registry=registry, record_event=event_log.append)

    start = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "sleep 0.05 && printf TOOL_COMPLETE", "yield_time_ms": 0},
        ),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]
    executor.execute(
        ToolCall(
            id="call:wait",
            name="terminal_process",
            arguments={"action": "wait", "process_id": process_id, "timeout_seconds": 2},
        ),
        event_context=CTX,
    )
    completion = next(event for event in event_log.iter() if isinstance(event, TerminalProcessCompletedEvent))

    assert completion.run_id == CTX.run_id
    assert completion.turn_id == CTX.turn_id
    assert completion.reply_id == CTX.reply_id
    assert completion.tool_call_id == "call:terminal"
    assert completion.process_id == process_id
    assert completion.output_preview == "TOOL_COMPLETE"


def test_terminal_tool_tty_is_valid_without_background_mode(tmp_path) -> None:
    _, result = execute_tool(
        tmp_path,
        "terminal",
        {"command": "python -c 'import sys; print(sys.stdin.isatty())'", "tty": True},
    )
    payload = json.loads(result.output)

    assert result.status is ToolResultState.SUCCESS
    assert payload["status"] == "success"
    assert payload["io_mode"] == "pty"
    assert "True" in payload["output"]


def test_terminal_tool_yielded_tty_reports_io_mode(tmp_path) -> None:
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    start = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={
                "command": "python -c 'import sys, time; print(sys.stdin.isatty(), flush=True); time.sleep(0.2)'",
                "yield_time_ms": 0,
                "tty": True,
            },
        ),
        event_context=CTX,
    )
    start_payload = json.loads(start.output)
    wait = executor.execute(
        ToolCall(
            id="call:wait",
            name="terminal_process",
            arguments={"action": "wait", "process_id": start_payload["process_id"], "timeout_seconds": 2},
        ),
        event_context=CTX,
    )
    wait_payload = json.loads(wait.output)

    assert start.status is ToolResultState.SUCCESS
    assert start_payload["io_mode"] == "pty"
    assert wait_payload["status"] == "success"
    assert wait_payload["io_mode"] == "pty"
    assert "True" in wait_payload["output"]


def test_todo_add_update_list_clear_and_validate_status(tmp_path) -> None:
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)

    added = executor.execute(
        ToolCall(id="call:todo:add", name="todo", arguments={"action": "add", "text": "write tests"}),
        event_context=CTX,
    )
    payload = json.loads(added.output)
    item_id = payload["items"][0]["id"]

    updated = executor.execute(
        ToolCall(
            id="call:todo:update",
            name="todo",
            arguments={"action": "update", "id": item_id, "status": "completed"},
        ),
        event_context=CTX,
    )
    assert json.loads(updated.output)["items"][0]["status"] == "completed"

    listed = executor.execute(
        ToolCall(id="call:todo:list", name="todo", arguments={"action": "list"}),
        event_context=CTX,
    )
    assert json.loads(listed.output)["items"][0]["text"] == "write tests"

    invalid = executor.execute(
        ToolCall(
            id="call:todo:bad",
            name="todo",
            arguments={"action": "add", "text": "bad", "status": "blocked"},
        ),
        event_context=CTX,
    )
    assert invalid.status is ToolResultState.ERROR
    assert "unsupported todo status" in invalid.output

    cleared = executor.execute(
        ToolCall(id="call:todo:clear", name="todo", arguments={"action": "clear"}),
        event_context=CTX,
    )
    assert json.loads(cleared.output)["items"] == []


def test_tool_executor_appends_tool_result_events_and_replays_message(tmp_path) -> None:
    registry = make_registry(tmp_path)
    event_log = InMemoryEventLog()
    executor = ToolExecutor(registry=registry, record_event=event_log.append)

    result = executor.execute(
        ToolCall(id="call:terminal", name="terminal", arguments={"command": "printf ok"}),
        event_context=CTX,
    )
    msg = event_log.replay("reply:tools")

    assert result.status is ToolResultState.SUCCESS
    assert [event.sequence for event in event_log.iter(reply_id="reply:tools")] == [1, 2, 3, 4, 5]
    assert isinstance(msg.content[0], ToolResultBlock)
    assert msg.content[0].name == "terminal"
    assert msg.content[0].state is ToolResultState.SUCCESS
    assert json.loads(msg.content[0].output[0].text)["output"] == "ok"


def test_tool_executor_archives_generic_large_output(tmp_path) -> None:
    class LargeOutputTool:
        name = "large_output"
        description = "Return a large plain text result."
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}
        is_read_only = True
        is_concurrency_safe = True

        def execute(self, call: ToolCall) -> ToolExecutionResult:
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                status=ToolResultState.SUCCESS,
                output="GENERIC_HEAD\n" + ("g" * 9000) + "\nGENERIC_TAIL",
            )

    runtime_session = in_memory_runtime_session(tmp_path)
    registry = ToolRegistry()
    registry.register(LargeOutputTool())
    executor = ToolExecutor(
        registry=registry,
        record_event=runtime_session.make_thread_recorder(),
        artifact_service=runtime_session.artifact_service,
    )

    result = executor.execute(ToolCall(id="call:large", name="large_output"), event_context=CTX)
    msg = runtime_session.event_log.replay("reply:tools")
    block = msg.content[0]

    assert result.status is ToolResultState.SUCCESS
    assert isinstance(block, ToolResultBlock)
    assert block.artifacts
    assert block.artifacts[0].role == "output"
    stored = runtime_session.archive.get_text(
        block.artifacts[0].artifact_id,
        session_id=runtime_session.runtime_session_id,
    )
    assert "GENERIC_HEAD" in stored
    assert "GENERIC_TAIL" in stored


def test_artifact_read_hides_cross_session_artifacts_with_not_found(tmp_path) -> None:
    archive = InMemoryArchiveStore()
    index = InMemoryToolResultArtifactIndex()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    session_a = in_memory_runtime_session(tmp_path / "a", archive=archive, tool_result_artifacts=index)
    session_b = in_memory_runtime_session(tmp_path / "b", archive=archive, tool_result_artifacts=index)
    executor_a = session_a.create_tool_executor(record_event=session_a.make_thread_recorder())
    executor_b = session_b.create_tool_executor(record_event=session_b.make_thread_recorder())

    terminal_result = executor_a.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={
                "command": "python -c 'print(\"OWNER_HEAD\"); print(\"o\" * 50000); print(\"OWNER_TAIL\")'",
                "max_output_chars": 200,
            },
        ),
        event_context=CTX,
    )
    assert terminal_result.status is ToolResultState.SUCCESS
    block = session_a.event_log.replay("reply:tools").content[0]
    assert isinstance(block, ToolResultBlock)
    artifact_id = block.artifacts[0].artifact_id

    missing = executor_b.execute(
        ToolCall(id="call:missing", name="artifact_read", arguments={"artifact_id": "artifact:missing"}),
        event_context=CTX,
    )
    forbidden = executor_b.execute(
        ToolCall(id="call:forbidden", name="artifact_read", arguments={"artifact_id": artifact_id}),
        event_context=CTX,
    )

    assert json.loads(missing.output)["status"] == "not_found"
    assert json.loads(forbidden.output) == json.loads(missing.output) | {"artifact_id": artifact_id}


def test_artifact_read_text_mode_rejects_binary_artifact(tmp_path) -> None:
    runtime_session = in_memory_runtime_session(tmp_path)
    artifact_id = "artifact:tool-result:run-tools:call-binary:screenshot:0"
    write = runtime_session.archive.put_bytes(
        artifact_id,
        b"\x89PNG",
        session_id=runtime_session.runtime_session_id,
        run_id=None,
        media_type="image/png",
    )
    runtime_session.tool_result_artifacts.put(
        ToolResultArtifactRecord(
            id="tool-result-artifact:run-tools:call-binary:screenshot:0",
            session_id=runtime_session.runtime_session_id,
            run_id="run:tools",
            turn_id="turn:tools",
            reply_id="reply:tools",
            tool_call_id="call:binary",
            tool_name="screenshot_tool",
            artifact_id=write.id,
            role="screenshot",
            ordinal=0,
            media_type="image/png",
            size_bytes=write.size_bytes,
        )
    )
    executor = runtime_session.create_tool_executor(record_event=runtime_session.make_thread_recorder())

    info = executor.execute(
        ToolCall(id="call:info", name="artifact_read", arguments={"artifact_id": artifact_id, "mode": "info"}),
        event_context=CTX,
    )
    text = executor.execute(
        ToolCall(id="call:text", name="artifact_read", arguments={"artifact_id": artifact_id}),
        event_context=CTX,
    )

    assert json.loads(info.output)["media_type"] == "image/png"
    text_payload = json.loads(text.output)
    assert text_payload["status"] == "error"
    assert "not a text artifact" in text_payload["error"]


def test_tool_result_artifact_options_reject_unrecoverable_threshold_band() -> None:
    with pytest.raises(ValueError, match="archive_threshold_bytes"):
        ToolResultArtifactOptions(archive_threshold_bytes=20_000, tool_result_message_context_chars=8_000)


def test_tool_result_artifact_service_uses_primary_full_text_for_adaptive_preview() -> None:
    archive = InMemoryArchiveStore()
    index = InMemoryToolResultArtifactIndex()
    service = ToolResultArtifactService(
        archive=archive,
        index=index,
        runtime_session_id="runtime:test",
        options=ToolResultArtifactOptions(archive_threshold_bytes=10),
    )
    full_output = "HEAD-" + ("x" * 40_000) + "-TAIL"
    result = ToolExecutionResult(
        call_id="call:terminal",
        tool_name="terminal",
        status=ToolResultState.SUCCESS,
        output=json.dumps({"status": "success", "output": "OLD_PREVIEW", "truncated": True}, ensure_ascii=False),
        artifact_candidates=(
            # Non-primary text artifact must not receive the primary output preview.
            ToolResultArtifactCandidate(role="diagnostics", media_type="text/plain; charset=utf-8", text="diag" * 20),
            ToolResultArtifactCandidate(role="combined_output", media_type="text/plain; charset=utf-8", text=full_output),
        ),
    )

    processed, refs = service.process_result(
        result,
        event_context=CTX,
        tool_call=ToolCall(id="call:terminal", name="terminal"),
    )

    payload = json.loads(processed.output)
    assert payload["output"] != "OLD_PREVIEW"
    assert payload["preview_policy"] == "head_tail"
    assert payload["output"].startswith("HEAD-")
    assert payload["output"].endswith("-TAIL")
    assert "OUTPUT TRUNCATED" in payload["output"]
    assert refs[0].preview is None
    assert refs[1].preview is not None
    assert refs[1].preview.read_more["artifact_id"] == refs[1].artifact_id
    record = index.get_for_session(refs[1].artifact_id, session_id="runtime:test")
    assert record is not None
    assert record.metadata["preview"] == refs[1].preview.model_dump()


def test_tool_result_artifact_service_archives_multibyte_text_by_bytes_but_previews_by_chars() -> None:
    archive = InMemoryArchiveStore()
    index = InMemoryToolResultArtifactIndex()
    service = ToolResultArtifactService(
        archive=archive,
        index=index,
        runtime_session_id="runtime:test",
        options=ToolResultArtifactOptions(archive_threshold_bytes=8_000),
    )
    text = "界" * 3_000
    processed, refs = service.process_result(
        ToolExecutionResult(
            call_id="call:tool",
            tool_name="lookup",
            status=ToolResultState.SUCCESS,
            output=text,
        ),
        event_context=CTX,
        tool_call=ToolCall(id="call:tool", name="lookup"),
    )

    assert len(text) < 8_000
    assert len(text.encode("utf-8")) > 8_000
    assert processed.output == text
    assert len(refs) == 1
    assert refs[0].preview is not None
    assert refs[0].preview.preview_policy == "full"
    assert refs[0].preview.original_chars == len(text)
    assert refs[0].preview.original_bytes == len(text.encode("utf-8"))


def test_terminal_streams_tool_result_delta_before_command_finishes(tmp_path) -> None:
    registry = make_registry(tmp_path)
    event_log = InMemoryEventLog()
    executor = ToolExecutor(registry=registry, record_event=event_log.append)
    result_holder = {}

    thread = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result",
            executor.execute(
                ToolCall(
                    id="call:terminal",
                    name="terminal",
                    arguments={
                        "command": (
                            "python -c 'import time; "
                            'print("STREAM_FIRST", flush=True); '
                            "time.sleep(1.0); "
                            'print("STREAM_SECOND", end="", flush=True)\''
                        )
                    },
                ),
                event_context=CTX,
            ),
        )
    )
    thread.start()
    deadline = time.monotonic() + 3.0
    saw_first_delta_before_end = False
    while time.monotonic() < deadline:
        events = list(event_log.iter(reply_id="reply:tools"))
        if any(
            isinstance(event, ToolResultTextDeltaEvent) and "STREAM_FIRST" in event.delta
            for event in events
        ) and not any(isinstance(event, ToolResultEndEvent) for event in events):
            saw_first_delta_before_end = True
            break
        time.sleep(0.02)
    thread.join(timeout=2)
    msg = event_log.replay("reply:tools")

    assert saw_first_delta_before_end is True
    assert thread.is_alive() is False
    assert result_holder["result"].status is ToolResultState.SUCCESS
    payload = json.loads(msg.content[0].output[0].text)
    assert payload["output"] == "STREAM_FIRST\nSTREAM_SECOND"


def test_terminal_streamed_json_deltas_match_final_result(tmp_path) -> None:
    registry = make_registry(tmp_path)
    event_log = InMemoryEventLog()
    executor = ToolExecutor(registry=registry, record_event=event_log.append)

    result = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={"command": "printf 'JSON_A\\n'; printf JSON_B"},
        ),
        event_context=CTX,
    )
    deltas = [
        event.delta
        for event in event_log.iter(reply_id="reply:tools")
        if isinstance(event, ToolResultTextDeltaEvent)
    ]
    streamed_json = "".join(deltas)

    assert json.loads(streamed_json) == json.loads(result.output)
    assert json.loads(streamed_json)["output"] == "JSON_A\nJSON_B"


def test_terminal_streaming_large_output_uses_conservative_live_head_then_tail(tmp_path) -> None:
    runtime_session, executor = make_runtime_executor(tmp_path)

    result = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={
                "command": "python -c 'print(\"HEAD\"); print(\"z\" * 60000); print(\"TAIL\")'",
            },
        ),
        event_context=CTX,
    )
    deltas = [
        event.delta
        for event in runtime_session.event_log.iter(reply_id="reply:tools")
        if isinstance(event, ToolResultTextDeltaEvent)
    ]
    streamed_json = "".join(deltas)
    streamed_payload = json.loads(streamed_json)
    end_event = next(
        event
        for event in runtime_session.event_log.iter(reply_id="reply:tools")
        if isinstance(event, ToolResultEndEvent) and event.tool_call_id == "call:terminal"
    )

    assert result.status is ToolResultState.SUCCESS
    assert len(streamed_json) < 15_000
    assert streamed_payload["preview_policy"] == "head_tail"
    assert streamed_payload["output"].startswith("HEAD")
    assert streamed_payload["output"].rstrip().endswith("TAIL")
    assert "OUTPUT TRUNCATED" in streamed_payload["output"]
    assert "artifact_id" not in streamed_payload["preview"]["read_more"]
    assert end_event.artifacts
    assert end_event.artifacts[0].preview is not None
    assert end_event.artifacts[0].preview.read_more["artifact_id"] == end_event.artifacts[0].artifact_id


def test_terminal_tiny_max_output_chars_is_clamped_for_artifact_budget(tmp_path) -> None:
    runtime_session, executor = make_runtime_executor(tmp_path)

    result = executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={
                "command": "python -c 'print(\"HEAD\"); print(\"z\" * 50000); print(\"TAIL\")'",
                "max_output_chars": 1,
            },
        ),
        event_context=CTX,
    )

    payload = json.loads(result.output)
    assert result.status is ToolResultState.SUCCESS
    assert payload["truncated"] is True
    end_event = next(
        event
        for event in runtime_session.event_log.iter(reply_id="reply:tools")
        if isinstance(event, ToolResultEndEvent) and event.tool_call_id == "call:terminal"
    )
    assert end_event.artifacts
    assert end_event.artifacts[0].preview is not None


def test_terminal_huge_streaming_head_matches_display_metadata(tmp_path) -> None:
    runtime_session, executor = make_runtime_executor(tmp_path)

    executor.execute(
        ToolCall(
            id="call:terminal",
            name="terminal",
            arguments={
                "command": "python -c 'print(\"H\" * 210000); print(\"TAIL\")'",
            },
        ),
        event_context=CTX,
    )
    streamed_json = "".join(
        event.delta
        for event in runtime_session.event_log.iter(reply_id="reply:tools")
        if isinstance(event, ToolResultTextDeltaEvent)
    )
    payload = json.loads(streamed_json)

    assert payload["preview_policy"] == "head_tail_huge"
    visible_head_chars = payload["preview"]["visible_head_chars"]
    assert payload["output"][visible_head_chars:].startswith("\n\n[OUTPUT TRUNCATED / PREVIEW")
    end_event = next(
        event
        for event in runtime_session.event_log.iter(reply_id="reply:tools")
        if isinstance(event, ToolResultEndEvent) and event.tool_call_id == "call:terminal"
    )
    assert end_event.artifacts[0].preview is not None
    assert end_event.artifacts[0].preview.visible_head_chars == visible_head_chars


def test_terminal_large_output_returns_preview_and_readable_artifact(tmp_path) -> None:
    runtime_session, executor = make_runtime_executor(tmp_path)

    result = executor.execute(
        ToolCall(
                id="call:terminal",
                name="terminal",
                arguments={
                    "command": "python -c 'print(\"HEAD\"); print(\"z\" * 50000); print(\"TAIL\")'",
                    "max_output_chars": 512,
                },
        ),
        event_context=CTX,
    )
    payload = json.loads(result.output)
    msg = runtime_session.event_log.replay("reply:tools")
    block = msg.content[0]
    assert isinstance(block, ToolResultBlock)
    replay_payload = json.loads(msg.content[0].output[0].text)

    assert result.status is ToolResultState.SUCCESS
    assert payload["truncated"] is True
    assert len(payload["output"]) < 700
    assert len(replay_payload["output"]) < 700
    assert block.artifacts
    artifact_id = block.artifacts[0].artifact_id

    read_result = executor.execute(
        ToolCall(
            id="call:artifact-read",
            name="artifact_read",
            arguments={"artifact_id": artifact_id, "max_chars": 100000},
        ),
        event_context=CTX,
    )
    read_payload = json.loads(read_result.output)
    assert read_payload["status"] == "success"
    assert "HEAD" in read_payload["text"]
    assert "TAIL" in read_payload["text"]


def test_terminal_process_log_artifact_metadata_uses_real_process_status(tmp_path) -> None:
    runtime_session, executor = make_runtime_executor(tmp_path)
    start = executor.execute(
        ToolCall(
            id="call:start",
            name="terminal",
            arguments={
                "command": "python -c 'import time; print(\"ERR_HEAD\"); print(\"e\" * 50000); print(\"ERR_TAIL\"); time.sleep(0.2); raise SystemExit(7)'",
                "yield_time_ms": 0,
                "max_output_chars": 200,
            },
        ),
        event_context=CTX,
    )
    process_id = json.loads(start.output)["process_id"]
    assert process_id
    executor.execute(
        ToolCall(
            id="call:wait",
            name="terminal_process",
            arguments={"action": "wait", "process_id": process_id, "timeout_seconds": 5, "max_output_chars": 200},
        ),
        event_context=CTX,
    )
    executor.execute(
        ToolCall(
            id="call:log",
            name="terminal_process",
            arguments={"action": "log", "process_id": process_id, "max_output_chars": 200},
        ),
        event_context=CTX,
    )

    msg = runtime_session.event_log.replay("reply:tools")
    log_block = next(block for block in msg.content if isinstance(block, ToolResultBlock) and block.id == "call:log")
    artifact_id = log_block.artifacts[0].artifact_id
    artifact_info = runtime_session.archive.get_info(
        artifact_id,
        session_id=runtime_session.runtime_session_id,
    )

    assert artifact_info.metadata["terminal_status"] == "error"


# --- Step 4.1: read-only allowlist drift guard + todo semantics -------------


def test_read_only_allowlist_matches_is_read_only_tools(tmp_path) -> None:
    # The contract's source of truth is the per-tool is_read_only flag; the gate
    # uses a name-set constant for self-containment. This test locks them
    # together: if a new read-only tool is added (or a flag flips) without
    # updating READ_ONLY_ALLOWED_TOOL_NAMES, this fails.
    from pulsara_agent.runtime.permission import READ_ONLY_ALLOWED_TOOL_NAMES

    class _StubService:
        """Registration-only stand-in; the drift test never executes tools."""

    registry = build_core_tool_registry(
        in_memory_runtime_session(tmp_path),
        memory_proposal_sink=MemoryProposalSink(),
        memory_recall_service=_StubService(),
        memory_query=_StubService(),
    )

    read_only_tool_names = {tool.name for tool in registry.all() if tool.is_read_only}
    assert read_only_tool_names == set(READ_ONLY_ALLOWED_TOOL_NAMES)


def test_todo_tool_is_read_only_true() -> None:
    # Semantic redefinition: todo only mutates agent-local ephemeral state, so
    # it is read-only for permission purposes (allowed under read-only mode).
    tool = TodoTool()
    assert tool.is_read_only is True
    # ...but it is NOT concurrency-safe (mutates shared _items), so the flip
    # does not let it run in parallel.
    assert tool.is_concurrency_safe is False
