from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from pulsara_agent.identity import random_id
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBudget,
    ToolResultPageBoundary,
)
from pulsara_agent.tools.builtins.filesystem import (
    FindFilesTool,
    SearchContentTool,
    ReadFileTool,
    EditFileTool,
)
from pulsara_agent.tools.builtins import filesystem_search as owner


def invoke(tool, arguments, **options):
    call = ToolCall(random_id("call"), tool.name, arguments, **options)
    result = tool.execute(call)
    assert result.page_complete
    assert ToolResultPageBudget(tool_call_id=call.id).fits(result.output)
    return json.loads(result.output)


def test_matching_excerpt_contains_late_match_and_source_can_be_read(tmp_path):
    line = "前" * 10_000 + "needle" + "尾" * 10_000
    (tmp_path / "long.txt").write_text(line)
    page = invoke(SearchContentTool(tmp_path), {"pattern": "needle"})
    record = page["matches"][0]
    assert "needle" in record["excerpt"]
    assert record["excerpt_partial"] and record["match_char_offset"] == 10_000
    assert record["line_chars"] == len(line)
    assert "content" not in record and "total_count" not in page
    read = invoke(ReadFileTool(tmp_path), {"target": record["target"]})
    assert read["next"] is not None


def test_excerpt_does_not_strip_a_real_carriage_return_match(tmp_path):
    (tmp_path / "crlf.txt").write_bytes(b"line\r\n")
    page = invoke(SearchContentTool(tmp_path), {"pattern": r"\r"})
    assert "\r" in page["matches"][0]["excerpt"]


def test_search_full_quote_and_continuations_cover_all_escaped_unicode_records(
    tmp_path,
):
    text = '\\"中🙂' * 200 + "needle"
    (tmp_path / "many.txt").write_text("\n".join(text for _ in range(120)))
    tool = SearchContentTool(tmp_path)
    arguments = {"pattern": "needle", "limit": 1000}
    lines = []
    while True:
        page = invoke(tool, arguments)
        lines.extend(record["line"] for record in page["matches"])
        if page["next"] is None:
            break
        assert page["next"]["arguments"]["limit"] == 1000
        arguments = page["next"]["arguments"]
    assert lines == list(range(1, 121))


def test_native_component_order_and_deleted_cursor_are_not_whole_path_sort(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.txt").write_text("hit")
    (tmp_path / "a+.txt").write_text("hit")
    first = invoke(FindFilesTool(tmp_path), {"glob": "*.txt", "limit": 1})
    assert first["targets"][0]["path"] == "a/b.txt"
    (tmp_path / "a/b.txt").unlink()
    second = invoke(FindFilesTool(tmp_path), first["next"]["arguments"])
    assert second["targets"] == [{"kind": "PATH", "path": "a+.txt"}]


def test_completed_file_content_is_not_searched_again(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hit\n")
    (tmp_path / "b.txt").write_text("hit\n")
    commands = []
    real = subprocess.Popen

    def popen(command, *args, **kwargs):
        commands.append(command)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(owner.subprocess, "Popen", popen)
    tool = SearchContentTool(tmp_path)
    first = invoke(tool, {"pattern": "hit", "output_mode": "files_only", "limit": 1})
    commands.clear()
    second = invoke(tool, first["next"]["arguments"])
    assert second["targets"][0]["path"] == "b.txt"
    searches = [command for command in commands if "-l" in command]
    assert searches and all("/a.txt" not in command for command in searches)
    assert any("/b.txt" in command for command in searches)


def test_literal_basename_batch_preserves_glob_ignore_binary_and_symlink_semantics(
    tmp_path,
):
    literal = "a[b]*?{x}.txt"
    (tmp_path / literal).write_text("hit")
    (tmp_path / "axbYz.txt").write_text("hit")
    (tmp_path / "ignored.txt").write_text("hit")
    (tmp_path / ".ignore").write_text("ignored.txt\n")
    (tmp_path / "binary.txt").write_bytes(b"\0hit\n")
    (tmp_path / "link.txt").symlink_to(tmp_path / literal)
    for mode in ("content", "files_only", "count"):
        page = invoke(
            SearchContentTool(tmp_path),
            {"pattern": "hit", "file_glob": "a*", "output_mode": mode},
        )
        key = {"content": "matches", "files_only": "targets", "count": "counts"}[mode]
        paths = [
            record["path"] if mode == "files_only" else record["target"]["path"]
            for record in page[key]
        ]
        assert paths == [literal, "axbYz.txt"]
    page = invoke(SearchContentTool(tmp_path), {"pattern": "hit"})
    assert {record["target"]["path"] for record in page["matches"]} == {
        literal,
        "axbYz.txt",
    }
    explicit = invoke(
        SearchContentTool(tmp_path),
        {"pattern": "hit", "target": {"kind": "PATH", "path": "binary.txt"}},
    )
    assert explicit["matches"]  # Preserve explicitly selected rg file semantics.


def test_best_effort_diagnostic_is_sticky_without_repeating_the_failed_page(
    tmp_path, monkeypatch
):
    (tmp_path / "a.txt").write_text("hit\n")
    (tmp_path / "b.txt").write_text("hit\n")
    original = owner._Producer._stderr

    def diagnostic(self):
        if "-l" in self.process.args:
            self.diagnostics.add(b"a local file could not be read\n")
        original(self)

    monkeypatch.setattr(owner._Producer, "_stderr", diagnostic)
    tool = SearchContentTool(tmp_path)
    first = invoke(tool, {"pattern": "hit", "output_mode": "files_only", "limit": 1})
    assert first["status"] == "partial" and first["targets"][0]["path"] == "a.txt"
    assert first["next"]["arguments"]["continuation"]["partial"] is True
    monkeypatch.setattr(owner._Producer, "_stderr", original)
    second = invoke(tool, first["next"]["arguments"])
    assert second["status"] == "partial" and second["targets"][0]["path"] == "b.txt"
    final = invoke(tool, second["next"]["arguments"])
    assert final["status"] == "partial" and final["next"] is None


def test_search_cancel_joins_every_child_and_worker_before_returning(
    tmp_path, monkeypatch
):
    executable = tmp_path / "sleeping-rg"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    executable.chmod(0o755)
    monkeypatch.setattr(owner, "private_ripgrep", lambda: executable)
    cancelled, started = threading.Event(), threading.Event()
    children = []
    real = subprocess.Popen

    def popen(*args, **kwargs):
        child = real(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(owner.subprocess, "Popen", popen)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            FindFilesTool(tmp_path).execute,
            ToolCall(
                random_id("call"),
                "find_files",
                {"glob": "*"},
                cancel_requested=cancelled.is_set,
            ),
        )
        assert started.wait(2)
        cancelled.set()
        with pytest.raises(InterruptedError, match="cancelled"):
            future.result(timeout=2)
    assert children and all(child.poll() is not None for child in children)


def test_small_call_allowance_does_not_skip_the_first_record(tmp_path):
    (tmp_path / "one.txt").write_text("hit")
    call = ToolCall(random_id("call"), "find_files", {"glob": "*.txt"})
    call = replace(
        call,
        page_budget=ToolResultPageBudget(
            tool_call_id=call.id, accepts_provider_body=lambda body, provenance: False
        ),
    )
    with pytest.raises(ToolResultPageBoundary):
        FindFilesTool(tmp_path).execute(call)


def test_actual_allowance_stops_before_searching_later_parent_batches(
    tmp_path, monkeypatch
):
    for name in ("a", "b", "c", "d"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "one.txt").write_text("hit")
    parents = []
    real = subprocess.Popen

    def popen(command, *args, **kwargs):
        if "--json" in command:
            parents.append(kwargs["cwd"].name)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(owner.subprocess, "Popen", popen)
    request = ToolCall(
        random_id("call"), "search_content", {"pattern": "hit", "limit": 50}
    )
    quotes = []

    def accepts(body, provenance):
        value = json.loads(body)
        quotes.append(value)
        return len(value["matches"]) <= 1

    budget = ToolResultPageBudget(
        tool_call_id=request.id, accepts_provider_body=accepts
    )
    result = SearchContentTool(tmp_path).execute(replace(request, page_budget=budget))
    value = json.loads(result.output)
    assert parents == ["a", "b"]
    assert [record["target"]["path"] for record in value["matches"]] == ["a/one.txt"]
    assert value["next"]["arguments"]["continuation"]["path"] == "a/one.txt"
    assert any(
        len(quote["matches"]) == 2 and quote["next"] is not None for quote in quotes
    )


@pytest.mark.parametrize("external", [False, True])
def test_producer_distinguishes_external_kill_from_its_own_page_stop(
    tmp_path, external
):
    request = ToolCall(random_id("call"), "find_files", {"glob": "*"})
    diagnostics = owner._Diagnostics(False)
    producer = owner._Producer(
        [
            sys.executable,
            "-u",
            "-c",
            "import time; print('record', flush=True); time.sleep(30)",
        ],
        cwd=tmp_path,
        call=request,
        diagnostics=diagnostics,
        deadline=time.monotonic() + 60,
    )
    try:
        assert next(producer.records(b"\n")) == b"record"
        assert not producer.natural
        if external:
            producer.process.kill()
            producer.process.wait()
    finally:
        code = producer.close()
    assert code < 0
    assert diagnostics.snapshot()[0] is external
    assert bool(diagnostics.snapshot()[1]) is external
    assert all(not thread.is_alive() for thread in producer.threads)


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        42,
        {},
        {"type": "other", "data": {}},
        {"type": "begin", "data": []},
        {"type": "begin", "data": {}},
        {"type": "summary", "data": {}},
        {
            "type": "match",
            "data": {
                "path": {"text": "file"},
                "lines": {"text": "hit"},
                "line_number": 1,
                "absolute_offset": 0,
                "submatches": [],
            },
        },
    ],
)
def test_malformed_rg_event_fails_without_leaving_any_producer(
    tmp_path, monkeypatch, malformed
):
    source = tmp_path / "one.txt"
    source.write_text("hit")
    executable = tmp_path / "faulty-rg"
    executable.write_text(
        f"#!{sys.executable}\n"
        + "import os,sys,time\n"
        + "if sys.argv[-1]=='-': sys.exit(1)\n"
        + f"if '--files' in sys.argv: os.write(1,{os.fsencode(str(source))!r}+b'\\0'); sys.exit(0)\n"
        + f"print({json.dumps(malformed)!r},flush=True)\n"
        + "time.sleep(30)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(owner, "private_ripgrep", lambda: executable)
    producers = []
    original = owner._Producer.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        producers.append(self)

    monkeypatch.setattr(owner._Producer, "__init__", initialize)
    with pytest.raises(ValueError, match="ripgrep"):
        SearchContentTool(tmp_path).execute(
            ToolCall(random_id("call"), "search_content", {"pattern": "hit"})
        )
    assert len(producers) == 3
    assert all(producer.process.poll() is not None for producer in producers)
    assert all(
        not thread.is_alive() for producer in producers for thread in producer.threads
    )


def test_decoder_accepts_all_control_events_from_actual_rg(tmp_path):
    completed = subprocess.run(
        [str(owner.private_ripgrep()), "--json", "-C", "1", "-e", "hit", "--", "-"],
        input=b"before\nhit\nafter\n",
        capture_output=True,
        check=True,
    )
    events = [owner._decode_event(line) for line in completed.stdout.splitlines()]
    assert {kind for kind, data in events} == {
        "begin",
        "context",
        "match",
        "end",
        "summary",
    }


def test_path_continuation_checks_revision_and_fragment_delivery(tmp_path):
    (tmp_path / "one.txt").write_text("x" * 80_000)
    tool = ReadFileTool(tmp_path)
    request = ToolCall(
        random_id("call"),
        "read_file",
        {"target": {"kind": "PATH", "path": "one.txt"}},
        defer_file_observation=True,
    )
    first = tool.execute(request)
    value = json.loads(first.output)
    assert value["next"]
    (tmp_path / "one.txt").write_text("changed")
    first.delivery_succeeded()
    denied = EditFileTool(tmp_path).execute(
        ToolCall(
            random_id("call"),
            "edit_file",
            {
                "target": {"kind": "PATH", "path": "one.txt"},
                "base_revision": value["content_revision"],
                "operations": [{"kind": "replace_file", "content": "replacement"}],
            },
        )
    )
    assert json.loads(denied.output)["error"] == "CONTENT_REVISION_MISMATCH"
    stale = tool.execute(
        ToolCall(random_id("call"), "read_file", value["next"]["arguments"])
    )
    assert json.loads(stale.output)["error"] == "CONTENT_REVISION_MISMATCH"
