"""Complete file production and process-local pending-log boundaries."""

from time import monotonic

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.ports.tool_output import ToolResultView
from pulsara_agent.tools.builtins.filesystem import (
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    FindFilesTool,
    SearchContentTool,
)
from pulsara_agent.terminal_process import TerminalManager, TerminalRequest
from pulsara_agent.terminal_process.output import TerminalOutputOwner


def call(tool, **args):
    result = tool.execute(ToolCall("provider-call", tool.name, args))
    if result.native_continuation is None:
        from pulsara_agent.tools.builtins.filesystem_contracts import file_tool_result_schema
        from pulsara_agent.ports.terminal import terminal_result_schema
        from pulsara_agent.conversation_kernel.tool_runtime_results import _kernel_tool_state
        from tests.support.tool_result_schemas import validate_business_result

        schema = (terminal_result_schema(tool.name)
            if tool.name in {"exec_process", "manage_process", "query_processes"}
            else file_tool_result_schema(tool.name, output_mode=args.get("output_mode", "content")))
        validate_business_result(schema, result.business_value,
            _kernel_tool_state(result.status))
    return result


def path(name):
    return {"kind": "PATH", "path": name}


def text(result):
    return "".join(
        part.value for part in result.business_value.content if part.type == "text"
    )


@pytest.mark.parametrize(
    ("body", "offset", "limit", "expected", "total"),
    [
        (b"a\r\nb\r\n", 1, None, "a\r\nb\r\n", 2),
        (b"a\nb\n", -1, None, "b\n", 2),
        (b"a\nb", -1, None, "b", 2),
        (b"a\n\n", -1, None, "\n", 2),
        (b"a\rb", 1, None, "a\rb", 1),
        (b"", -100, None, "", 0),
        (b"\xef\xbb\xbf", 1, None, "", 0),
        (b"a\nb\nc\n", -2, 1, "b\n", 3),
        (b"a\n", 10, None, "", 1),
    ],
)
def test_read_file_freezes_exact_lf_range(
    tmp_path, body, offset, limit, expected, total
):
    (tmp_path / "a.txt").write_bytes(body)
    args = {"target": path("a.txt"), "offset": offset}
    if limit is not None:
        args["limit"] = limit
    result = call(ReadFileTool(tmp_path), **args)
    assert result.status is ToolResultState.SUCCESS
    assert text(result) == expected
    assert result.business_value.public_fields == {
        "target": path("a.txt"),
        "meta": {
            "total_lines": total,
            "file_size": len(body),
            "had_utf8_bom": body.startswith(b"\xef\xbb\xbf"),
        },
    }
    assert bool(result.business_value.content) == bool(expected)


def test_read_full_more_than_old_line_limit_and_reject_old_paging(tmp_path):
    body = "line\n" * 4001
    (tmp_path / "a.txt").write_text(body)
    tool = ReadFileTool(tmp_path)
    assert text(call(tool, target=path("a.txt"))) == body
    assert text(call(tool, target=path("a.txt"), limit=3000)) == "line\n" * 3000
    for args in (
        {"offset": 0},
        {"limit": 0},
        {"continuation": {}},
        {"offset": None},
        {"limit": None},
    ):
        with pytest.raises(ValueError):
            call(tool, target=path("a.txt"), **args)


def test_edit_unique_match_without_reading_and_preserve_bom_crlf(tmp_path):
    file = tmp_path / "a.txt"
    file.write_bytes(b"\xef\xbb\xbfA\r\nB\r\n")
    tool = EditFileTool(tmp_path)
    result = call(tool, target=path("a.txt"), old_text="B\r\n", new_text="C\n")
    assert result.status is ToolResultState.SUCCESS
    assert file.read_bytes() == b"\xef\xbb\xbfA\r\nC\n"
    assert "-B\r\n+C\n" in text(result)
    before = file.stat().st_mtime_ns
    noop = call(tool, target=path("a.txt"), old_text="C\n", new_text="C\n")
    assert noop.business_value.content == () and file.stat().st_mtime_ns == before
    whole = call(tool, target=path("a.txt"), content="plain")
    assert file.read_bytes() == b"plain"
    assert "\\ No newline at end of file" in text(whole)


def test_edit_rejects_overlap_and_empty_old_for_nonempty_file(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("aaa")
    tool = EditFileTool(tmp_path)
    for old, new in (("aa", "bb"), ("missing", "missing"), ("", "x")):
        result = call(tool, target=path("a.txt"), old_text=old, new_text=new)
        assert result.status is ToolResultState.ERROR and file.read_text() == "aaa"
    file.write_bytes(b"\xef\xbb\xbf")
    assert (
        call(tool, target=path("a.txt"), old_text="", new_text="x").status
        is ToolResultState.SUCCESS
    )
    assert file.read_bytes() == b"\xef\xbb\xbfx"


def test_write_create_only_and_full_result_export_bypasses_text_filter(tmp_path):
    class Reader:
        def read_result(self, target):
            assert target == {"kind": "SESSION_ENTRY", "entry_id": entry}
            return ToolResultView(
                target,
                {
                    "meta": {"status": "ERROR"},
                    "content": [
                        {"type": "text", "value": "x\x00y\r\n"},
                        {"type": "object", "value": {"n": [True, None]}},
                    ],
                },
            )

    entry = random_id("entry")
    tool = WriteFileTool(tmp_path, result_source_reader=Reader())
    good = call(tool, target=path("a.txt"), content="x\r\n")
    assert good.business_value.public_fields["meta"]["bytes_written"] == 3
    assert (tmp_path / "a.txt").read_bytes() == b"x\r\n"
    assert (
        call(tool, target=path("a.txt"), content="other").status
        is ToolResultState.ERROR
    )
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    assert (
        call(tool, target=path("dangling"), content="x").status is ToolResultState.ERROR
    )
    exported = call(
        tool,
        target=path("nested/export.bin"),
        tool_result_target={"kind": "SESSION_ENTRY", "entry_id": entry},
    )
    data = (tmp_path / "nested/export.bin").read_bytes()
    assert exported.status is ToolResultState.SUCCESS and b"x\x00y\r\n" in data
    assert exported.business_value.public_fields["meta"]["bytes_written"] == len(data)
    assert (
        call(tool, target=path("ordinary.bin"), content="x").status
        is ToolResultState.ERROR
    )


def test_search_complete_long_lines_count_occurrences_and_files(tmp_path):
    line = "prefix" + "x" * 1000 + " foo foo\r\n"
    (tmp_path / "a.txt").write_text(line + "foo\n")
    tool = SearchContentTool(tmp_path)
    result = call(tool, pattern="foo")
    assert [p.value for p in result.business_value.content] == [
        {"target": path("a.txt"), "line": 1, "text": line},
        {"target": path("a.txt"), "line": 2, "text": "foo\n"},
    ]
    assert call(tool, pattern="foo", output_mode="count").business_value.content[
        0
    ].value == {"target": path("a.txt"), "count": 3}
    assert (
        call(tool, pattern="foo", output_mode="files_only")
        .business_value.content[0]
        .type
        == "target"
    )
    with pytest.raises(ValueError):
        call(tool, pattern="foo", limit=1)


def test_find_files_exceeds_old_result_cap(tmp_path):
    for i in range(1005):
        (tmp_path / f"f{i:04}.txt").write_text("x")
    result = call(FindFilesTool(tmp_path), glob="*.txt")
    assert (
        result.status is ToolResultState.SUCCESS
        and len(result.business_value.content) == 1005
    )
    assert result.business_value.content[-1].value == path("f1004.txt")


def test_terminal_spill_keeps_all_bytes_and_accepted_position_only():
    owner = TerminalOutputOwner(
        owner_epoch=random_id("host"), process_id=random_id("process"), maximum_bytes=8
    )
    body = ("汉🙂\n" * 1000).encode()
    owner.append_raw(body)
    owner.finalize(status="success", exit_code=0)
    assert owner.retained_utf8_bytes == 0
    original = owner.observation_cursor()
    snap, _ = owner.snapshot_with_body_candidate(
        maximum_chars=None, since_cursor=original
    )
    assert snap.text.encode() == body and not snap.gap_before_output
    assert (
        owner.snapshot_with_body_candidate(
            maximum_chars=None, since_cursor=owner.observation_cursor()
        )[0].text
        == snap.text
    )
    owner.accept_observation(snap.output_cursor)
    assert (
        owner.snapshot_with_body_candidate(
            maximum_chars=None, since_cursor=owner.observation_cursor()
        )[0].text
        == ""
    )
    owner.close_reads_if_idle()


def test_real_terminal_nonzero_exit_full_unobserved_snapshot(tmp_path):
    manager = TerminalManager(tmp_path)
    owner = random_id("host")
    manager.activate_owner(owner)
    try:
        result = manager.execute(
            TerminalRequest("printf 'first\\nsecond\\n'; exit 7"),
            owner_host_session_id=owner,
            decision_deadline_monotonic=monotonic() + 30,
        )
        assert result.exit_code == 7 and result.output == "first\nsecond\n"
        before = manager.model_observation_cursor(
            result.process_id, owner_host_session_id=owner
        )
        same = manager.wait_process(
            result.process_id,
            timeout_seconds=0,
            max_output_chars=None,
            since_cursor=before,
            owner_host_session_id=owner,
        )
        assert same.output == result.output
        manager.accept_model_observation(
            result.process_id, result.output_cursor, owner_host_session_id=owner
        )
        empty = manager.wait_process(
            result.process_id,
            timeout_seconds=0,
            max_output_chars=None,
            since_cursor=manager.model_observation_cursor(
                result.process_id, owner_host_session_id=owner
            ),
            owner_host_session_id=owner,
        )
        assert empty.output == "" and empty.exit_code == 7
    finally:
        manager.release_owner_and_join(owner)


def test_native_next_reads_only_frozen_result_and_js_schema_rejects_it(tmp_path):
    from jsonschema import Draft202012Validator
    from pulsara_agent.ports.tool_output import NativeToolContinuation
    from pulsara_agent.tools.builtins.filesystem_contracts import (
        read_file_parameters_schema,
    )

    target = {"kind": "NEXT", "token": "opaque-runtime-token"}
    source = ToolResultView(
        {"kind": "SESSION_ENTRY", "entry_id": random_id("entry")},
        {
            "meta": {"status": "SUCCESS"},
            "content": [{"type": "text", "value": "frozen"}],
        },
    )
    calls = []

    class Reader:
        def read_native_page(self, value):
            calls.append(value)
            return NativeToolContinuation(source, (1, 0))

    tool = ReadFileTool(tmp_path, result_source_reader=Reader())
    result = call(tool, target=target)
    assert result.native_continuation.source is source
    assert calls == [target] and list(tmp_path.iterdir()) == []
    assert result.business_value.content == ()
    assert not list(
        Draft202012Validator(read_file_parameters_schema()).iter_errors(
            {"target": target}
        )
    )
    assert list(
        Draft202012Validator(read_file_parameters_schema(native=False)).iter_errors(
            {"target": target}
        )
    )
    with pytest.raises(ValueError):
        call(tool, target=target, limit=1)


def test_terminal_declared_value_separates_exit_status_and_real_failure():
    from pulsara_agent.conversation_kernel.tool_runtime_terminal import (
        _terminal_execution_result,
    )
    from pulsara_agent.terminal_process import TerminalResult, TerminalStatus

    result = TerminalResult(
        TerminalStatus.ERROR, "output", 7, "/tmp", process_id=random_id("process")
    )
    calls = []
    declared = _terminal_execution_result(
        ToolCall("call", "exec_process"),
        result,
        acceptance_succeeded=lambda: calls.append("accepted"),
    )
    assert declared.status is ToolResultState.SUCCESS
    assert declared.business_value.public_fields["meta"] == {
        "process_status": "EXITED",
        "exit_code": 7,
        "cwd": "/tmp",
    }
    assert calls == []
    declared.acceptance_succeeded()
    assert calls == ["accepted"]
    failed = _terminal_execution_result(
        ToolCall("call", "exec_process"),
        TerminalResult(TerminalStatus.ERROR, "", -1, "/tmp", error="spawn failed"),
    )
    assert failed.status is ToolResultState.ERROR
    assert failed.business_value.public_fields == {}
    assert text(failed) == "spawn failed"


def test_terminal_model_tools_isolate_sessions_on_the_shared_host(tmp_path):
    from pulsara_agent.conversation_kernel.tool_runtime import (
        _DirectTerminalProcessTool,
        _DirectTerminalQueryTool,
    )
    from pulsara_agent.terminal_process.models import TerminalProcessOrigin

    manager = TerminalManager(tmp_path)
    owner, first, second = random_id("host"), random_id("session"), random_id("session")
    manager.activate_owner(owner)
    try:
        observed = manager.execute(
            TerminalRequest("printf own-session"),
            owner_host_session_id=owner,
            origin=TerminalProcessOrigin(random_id("turn"), "ROOT", first),
            decision_deadline_monotonic=monotonic() + 30,
        )
        target = {"kind": "PROCESS", "process_id": observed.process_id}
        first_query = _DirectTerminalQueryTool(manager, owner, session_id=first)
        second_query = _DirectTerminalQueryTool(manager, owner, session_id=second)
        assert call(first_query).business_value.content[0].value["target"] == target
        assert call(second_query).business_value.content[0].type == "text"
        assert call(second_query, target=target).status is ToolResultState.ERROR
        with pytest.raises(KeyError):
            call(
                _DirectTerminalProcessTool(manager, owner, session_id=second),
                target=target,
                action="wait",
                timeout_seconds=0,
            )
        own = call(
            _DirectTerminalProcessTool(manager, owner, session_id=first),
            target=target,
            action="wait",
            timeout_seconds=0,
        )
        assert text(own) == "own-session"
        own.acceptance_succeeded()
        assert (
            text(
                call(
                    _DirectTerminalProcessTool(manager, owner, session_id=first),
                    target=target,
                    action="wait",
                    timeout_seconds=0,
                )
            )
            == ""
        )
    finally:
        manager.release_owner_and_join(owner)
