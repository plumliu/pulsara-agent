"""Closed query branches and read-only observations of the existing terminal owners."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from time import monotonic

from jsonschema import Draft202012Validator
import pytest
from pydantic import ValidationError

from pulsara_agent.identity import random_id
from pulsara_agent.ports.terminal import (
    QueryProcessInput,
    QueryProcessesListInput,
    parse_exec_process_input,
    parse_manage_process_input,
    parse_query_processes_input,
    exec_process_input_schema,
    manage_process_input_schema,
    query_processes_input_schema,
)
from pulsara_agent.terminal_process.manager import TerminalManager


PROCESS = {"kind": "PROCESS", "process_id": random_id("process")}
CELL = {"kind": "CELL", "cell_id": random_id("cell")}


@pytest.mark.parametrize(
    ("parser", "schema", "valid", "invalid"),
    [
        (
            parse_exec_process_input,
            exec_process_input_schema,
            [{"command": "true"}, {"command": "true", "workdir": None}],
            [{"command": "true", "target": PROCESS}, {"command": "true", "tty": 1}],
        ),
        (
            parse_query_processes_input,
            query_processes_input_schema,
            [{}, {"include_finished": False}, {"target": PROCESS}],
            [
                {"target": None},
                {"target": PROCESS, "include_running": True},
                {"target": CELL},
                {"action": "list"},
                {"cursor": "unused"},
                {"include_running": None},
                {"include_finished": 1},
            ],
        ),
        (
            parse_manage_process_input,
            manage_process_input_schema,
            [
                {"action": "poll", "target": PROCESS, "since_cursor": None},
                {"action": "wait", "target": PROCESS, "timeout_seconds": 1},
                {"action": "write", "target": PROCESS, "data": ""},
                {"action": "submit", "target": PROCESS, "data": "line\n"},
                {"action": "close_stdin", "target": PROCESS},
                {"action": "kill", "target": PROCESS},
            ],
            [
                {"action": "list"},
                {"action": "poll", "target": PROCESS, "timeout_seconds": 1},
                {"action": "kill", "target": PROCESS, "data": ""},
                {"action": "wait", "target": PROCESS, "timeout_seconds": True},
                {"action": "write", "target": CELL, "data": ""},
                {"action": "submit", "target": PROCESS, "data": None},
            ],
        ),
    ],
)
def test_terminal_query_canonical_schema_matches_parser(parser, schema, valid, invalid):
    validator = Draft202012Validator(schema())
    for arguments in valid:
        validator.validate(arguments)
        parser(arguments)
    for arguments in invalid:
        assert not validator.is_valid(arguments), arguments
        with pytest.raises(ValidationError):
            parser(arguments)


def test_query_process_defaults_apply_only_to_list():
    listing = parse_query_processes_input({})
    assert isinstance(listing, QueryProcessesListInput)
    assert listing.include_running is True and listing.include_finished is True
    detail = parse_query_processes_input({"target": PROCESS})
    assert isinstance(detail, QueryProcessInput)
    assert detail.model_dump() == {"target": PROCESS}


@pytest.fixture
def live_process(tmp_path: Path):
    manager = TerminalManager(tmp_path)
    owner = random_id("host")
    manager.activate_owner(owner)
    state, yielded = manager.process_registry.exec_with_yield(
        command="query metadata test",
        cwd=tmp_path,
        yield_time_ms=0,
        tty=False,
        max_lifetime_seconds=None,
        owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        decision_deadline_monotonic=monotonic() + 5,
        env=dict(os.environ),
    )
    assert yielded
    try:
        yield manager, owner, state
    finally:
        manager.release_owner(owner, timeout_seconds=5)


def test_process_detail_reuses_info_snapshot_and_never_polls(live_process, monkeypatch):
    manager, owner, state = live_process
    state.output.append_raw(b"retained log body\n")
    before = state.output.snapshot(maximum_chars=100)
    snapshot = state.output.snapshot
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        return snapshot(**kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("metadata query must not poll or read a process log")

    monkeypatch.setattr(state.output, "snapshot", capture)
    monkeypatch.setattr(manager.process_registry, "poll", forbidden)
    monkeypatch.setattr(manager.process_registry, "log", forbidden)
    detail = manager.get_process_info(state.process_id, owner_host_session_id=owner)
    assert calls == [{"maximum_chars": 1}]
    assert detail.command == "query metadata test"
    assert "output" not in detail.to_payload()
    after = snapshot(maximum_chars=100)
    assert after.text == before.text == "retained log body\n"
    assert after.retained_from_cursor == before.retained_from_cursor
    assert after.output_cursor == before.output_cursor
    with pytest.raises(KeyError):
        manager.get_process_info(state.process_id, owner_host_session_id=random_id("host"))
    with pytest.raises(KeyError):
        manager.get_process_info(random_id("process"), owner_host_session_id=owner)
