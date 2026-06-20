import json
import threading
import time

from pulsara_agent.event import EventContext, ToolResultEndEvent, ToolResultTextDeltaEvent
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.message import ToolResultBlock, ToolResultState
from pulsara_agent.runtime import RuntimeSession
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.tools import ToolCall, ToolExecutor, build_core_tool_registry


CTX = EventContext(run_id="run:tools", turn_id="turn:tools", reply_id="reply:tools")


def make_registry(tmp_path):
    return build_core_tool_registry(RuntimeSession(tmp_path))


def execute_tool(tmp_path, name: str, arguments: dict) -> tuple[ToolExecutor, object]:
    registry = make_registry(tmp_path)
    executor = ToolExecutor(registry=registry)
    result = executor.execute(
        ToolCall(id=f"call:{name}", name=name, arguments=arguments),
        event_context=CTX,
    )
    return executor, result


def test_core_tool_registry_exposes_minimal_builtin_tools(tmp_path) -> None:
    registry = make_registry(tmp_path)

    assert registry.names() == [
        "edit_file",
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
        RuntimeSession(tmp_path),
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


def test_read_file_reads_workspace_file_and_blocks_path_escape(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello Pulsara\nsecond line\n", encoding="utf-8")

    _, result = execute_tool(tmp_path, "read_file", {"path": "note.txt", "offset": 2, "limit": 1})

    assert result.status is ToolResultState.SUCCESS
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["path"] == "note.txt"
    assert payload["offset"] == 2
    assert payload["total_lines"] == 2
    assert payload["content"] == "2|second line"

    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    _, escaped = execute_tool(tmp_path, "read_file", {"path": str(outside)})

    assert escaped.status is ToolResultState.ERROR
    assert "escapes workspace root" in escaped.output


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
    payload = json.loads(result.output)
    assert payload["total_count"] == 1
    assert payload["matches"][0]["path"].endswith("memory.md")
    assert payload["matches"][0]["content"] == "JSON-LD memory"


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


def test_terminal_dangerous_command_called_directly_fails_closed(tmp_path) -> None:
    _, result = execute_tool(tmp_path, "terminal", {"command": "rm -rf build"})
    payload = json.loads(result.output)

    assert result.status is ToolResultState.ERROR
    assert payload["status"] == "blocked"
    assert payload["policy_code"] == "requires_confirmation"
    assert "requires user confirmation" in payload["error"]


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


def test_terminal_large_output_returns_preview_and_readable_full_output_ref(tmp_path) -> None:
    registry = make_registry(tmp_path)
    event_log = InMemoryEventLog()
    executor = ToolExecutor(registry=registry, record_event=event_log.append)

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
    msg = event_log.replay("reply:tools")
    replay_payload = json.loads(msg.content[0].output[0].text)

    assert result.status is ToolResultState.SUCCESS
    assert payload["truncated"] is True
    assert payload["full_output_ref"]
    assert len(payload["output"]) < 700
    assert len(replay_payload["output"]) < 700
    assert replay_payload["full_output_ref"] == payload["full_output_ref"]

    read_result = executor.execute(
        ToolCall(
            id="call:read",
            name="read_file",
            arguments={"path": payload["full_output_ref"]},
        ),
        event_context=CTX,
    )
    read_payload = json.loads(read_result.output)
    assert "HEAD" in read_payload["content"]
    assert "TAIL" in read_payload["content"]
