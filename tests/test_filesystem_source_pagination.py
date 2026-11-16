"""Complete ripgrep scopes retain SDK matching and subprocess cleanup semantics."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import threading
import time
import pytest
from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.tools.builtins.filesystem import (
    FindFilesTool,
    SearchContentTool,
    ReadFileTool,
)
from pulsara_agent.tools.builtins import filesystem_search as owner


def invoke(tool, arguments):
    result = tool.execute(ToolCall(random_id("call"), tool.name, arguments))
    assert result.status is ToolResultState.SUCCESS
    return result.business_value.content


def test_matching_line_is_complete_and_source_read_is_exact(tmp_path):
    line = "前" * 10_000 + "needle" + "尾" * 10_000
    (tmp_path / "long.txt").write_text(line)
    record = invoke(SearchContentTool(tmp_path), {"pattern": "needle"})[0].value
    assert record == {
        "target": {"kind": "PATH", "path": "long.txt"},
        "line": 1,
        "text": line,
    }
    assert invoke(ReadFileTool(tmp_path), {"target": record["target"]})[0].value == line


def test_real_carriage_return_and_every_unicode_match_are_preserved(tmp_path):
    text = '\\"中🙂' * 200 + "needle\r\n"
    (tmp_path / "many.txt").write_bytes((text * 120).encode())
    parts = invoke(SearchContentTool(tmp_path), {"pattern": r"needle\r"})
    assert [p.value["line"] for p in parts] == list(range(1, 121))
    assert all(p.value["text"] == text for p in parts)


def test_full_search_reads_each_selected_file_once(tmp_path, monkeypatch):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("hit\n")
    commands = []
    real = subprocess.Popen

    def popen(command, *args, **kwargs):
        commands.append(command)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(owner.subprocess, "Popen", popen)
    parts = invoke(
        SearchContentTool(tmp_path), {"pattern": "hit", "output_mode": "files_only"}
    )
    assert [p.value["path"] for p in parts] == ["a.txt", "b.txt"]
    searches = [c for c in commands if "--json" in c]
    assert len(searches) == 1


def test_full_diagnostic_is_an_error_with_obtained_records(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hit\n")
    original = owner._Producer._stderr
    diagnostic = b"a local file could not be read " * 1000

    def stderr(self):
        if "--json" in self.process.args:
            self.diagnostics.add(diagnostic)
        original(self)

    monkeypatch.setattr(owner._Producer, "_stderr", stderr)
    result = SearchContentTool(tmp_path).execute(
        ToolCall(random_id("call"), "search_file_content", {"pattern": "hit"})
    )
    assert result.status is ToolResultState.ERROR
    assert result.business_value.content[0].value["target"]["path"] == "a.txt"
    assert diagnostic.decode() in result.business_value.content[-1].value


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

        paths = [
            record.value["path"]
            if mode == "files_only"
            else record.value["target"]["path"]
            for record in page
        ]
        assert paths == [literal, "axbYz.txt"]
    page = invoke(SearchContentTool(tmp_path), {"pattern": "hit"})
    assert {record.value["target"]["path"] for record in page} == {
        literal,
        "axbYz.txt",
    }
    explicit = invoke(
        SearchContentTool(tmp_path),
        {"pattern": "hit", "target": {"kind": "PATH", "path": "binary.txt"}},
    )
    assert explicit  # Preserve explicitly selected rg file semantics.


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
        result = future.result(timeout=2)
        assert result.status is ToolResultState.INTERRUPTED
        assert "cancelled" in result.business_value.content[-1].value
    assert children and all(child.poll() is not None for child in children)


@pytest.mark.parametrize("external", [False, True])
def test_producer_distinguishes_external_kill_from_its_own_cleanup(tmp_path, external):
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
    result = SearchContentTool(tmp_path).execute(
        ToolCall(random_id("call"), "search_file_content", {"pattern": "hit"})
    )
    assert result.status is ToolResultState.ERROR
    assert "ripgrep" in result.business_value.content[-1].value
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
