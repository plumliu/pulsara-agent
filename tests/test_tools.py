import json

from pulsara_agent.event import EventContext
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.message import ToolResultBlock, ToolResultState
from pulsara_agent.runtime import RuntimeSession
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
        "todo",
        "write_file",
    ]
    assert [spec.name for spec in registry.tool_specs()] == registry.names()
    assert all(spec.parameters["type"] == "object" for spec in registry.tool_specs())


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
    assert "session_id" in terminal_spec.parameters["properties"]
    assert result.status is ToolResultState.SUCCESS
    assert payload["output"] == str(tmp_path / "src")
    assert payload["terminal_session_id"] == "default"
    assert payload["backend_type"] == "local"
    assert result.metadata["cwd"] == str(tmp_path / "src")
    assert result.metadata["terminal_session_id"] == "default"
    assert result.metadata["backend_type"] == "local"


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
    assert [event.sequence for event in event_log.iter(reply_id="reply:tools")] == [1, 2, 3]
    assert isinstance(msg.content[0], ToolResultBlock)
    assert msg.content[0].name == "terminal"
    assert msg.content[0].state is ToolResultState.SUCCESS
    assert json.loads(msg.content[0].output[0].text)["output"] == "ok"
