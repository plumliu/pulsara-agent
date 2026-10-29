"""Closed query branches and read-only observations of the existing terminal owners."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys
from time import monotonic

from jsonschema import Draft202012Validator
import pytest
from pydantic import ValidationError

from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.identity import random_id
from pulsara_agent.ports.terminal import (
    QueryProcessInput,
    QueryProcessesListInput,
    parse_exec_process_input,
    parse_manage_monitor_input,
    parse_manage_process_input,
    parse_query_monitors_input,
    parse_query_processes_input,
    exec_process_input_schema,
    manage_monitor_input_schema,
    manage_process_input_schema,
    query_monitors_input_schema,
    query_processes_input_schema,
)
from pulsara_agent.terminal_process.manager import TerminalManager
from pulsara_agent.terminal_process.monitor import (
    TerminalMonitorCoordinator,
    TerminalMonitorPolicy,
)


PROCESS = {"kind": "PROCESS", "process_id": random_id("process")}
MONITOR = {"kind": "MONITOR", "monitor_id": random_id("monitor")}


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
                {"target": MONITOR},
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
                {"action": "write", "target": MONITOR, "data": ""},
                {"action": "submit", "target": PROCESS, "data": None},
            ],
        ),
        (
            parse_query_monitors_input,
            query_monitors_input_schema,
            [{}, {"target": MONITOR}],
            [
                {"target": None},
                {"target": PROCESS},
                {"target": MONITOR, "cursor": "unused"},
                {"action": "list"},
                {"max_items": 1},
            ],
        ),
        (
            parse_manage_monitor_input,
            manage_monitor_input_schema,
            [
                {"action": "register", "target": PROCESS},
                {"action": "register", "target": PROCESS, "conditions": {"output": None}},
                {"action": "cancel", "target": MONITOR},
            ],
            [
                {"action": "list"},
                {"action": "register", "target": PROCESS, "conditions": None},
                {"action": "cancel", "target": MONITOR, "lifetime": {}},
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


@pytest.mark.parametrize("with_output", [False, True])
@pytest.mark.parametrize("close_kind", ["cancel", "work", "host"])
def test_monitor_detail_projects_live_policy_without_mutating_registration(
    live_process, monkeypatch, with_output, close_kind
):
    manager, owner, state = live_process
    agent_id, work_turn_id = random_id("session"), random_id("turn")
    coordinator = TerminalMonitorCoordinator(
        session_id=random_id("session"),
        owner_epoch=owner,
        registry=manager.process_registry,
        live_bus=LiveAgentEventBus(),
        wake_scheduler=lambda: None,
    )
    try:
        prepared = coordinator.prepare_registration(
            process_id=state.process_id,
            origin_turn_id=work_turn_id,
            origin_attempt_id=random_id("attempt"),
            origin_result_entry_id=random_id("entry"),
            writer_generation=1,
            authorization_reference="test:query",
            policy=TerminalMonitorPolicy(),
            registering_agent_id=agent_id,
            work_turn_id=work_turn_id,
        )
        registration = coordinator._registrations[prepared.monitor_id]
        # Query reads the effective current policy, not the original input arguments.
        registration.policy = replace(
            registration.policy,
            min_new_output_chars=321 if with_output else None,
            quiet_period_ms=89,
            heartbeat_interval_seconds=17,
            max_output_chars=1024,
            minimum_progress_interval_seconds=11,
            maximum_duration_seconds=123,
        )
        coordinator.settle_registration(prepared, committed=True)
        coordinator._evaluate(prepared.monitor_id, monotonic() + 18)
        assert registration.draft is not None
        before = (
            registration.expires_at_monotonic,
            registration.last_accepted_cursor,
            registration.state,
            registration.draft,
            registration.in_flight,
            registration.successor,
            registration.observation_ordinal,
        )

        def forbidden(*args, **kwargs):
            pytest.fail("monitor query must not sample or consume process output")

        monkeypatch.setattr(manager.process_registry, "snapshot_and_subscribe", forbidden)
        monkeypatch.setattr(state.output, "snapshot", forbidden)
        detail = coordinator.inspect_current(prepared.monitor_id)
        assert detail is not None
        assert detail["pending"] is True
        assert detail["conditions"] == {
            "completion": True,
            "output": {"min_new_output_chars": 321, "quiet_period_ms": 89} if with_output else None,
            "heartbeat_interval_seconds": 17,
        }
        assert detail["delivery"] == {
            "max_output_chars": 1024,
            "minimum_progress_observation_interval_seconds": 11,
        }
        assert detail["lifetime"] == {"maximum_duration_seconds": 123}
        assert {key: detail[key] for key in coordinator.list_current()[0]} == coordinator.list_current()[0]
        assert coordinator.inspect_current(
            prepared.monitor_id, agent_id=agent_id, work_turn_id=work_turn_id
        ) == detail
        assert coordinator.inspect_current(
            prepared.monitor_id, agent_id=agent_id, work_turn_id=random_id("turn")
        ) is None
        assert coordinator.inspect_current(
            prepared.monitor_id, agent_id=random_id("session"), work_turn_id=work_turn_id
        ) is None
        assert coordinator.inspect_current(random_id("monitor")) is None
        assert before == (
            registration.expires_at_monotonic,
            registration.last_accepted_cursor,
            registration.state,
            registration.draft,
            registration.in_flight,
            registration.successor,
            registration.observation_ordinal,
        )
        if close_kind == "cancel":
            assert coordinator.cancel(prepared.monitor_id) == "cancelled"
        elif close_kind == "work":
            coordinator.close_work(work_turn_id)
        else:
            coordinator.stop_admission_and_close(timeout_seconds=5)
        assert coordinator.inspect_current(prepared.monitor_id) is None
        assert coordinator.list_current() == ()
        assert state.process.poll() is None
    finally:
        coordinator.stop_admission_and_close(timeout_seconds=5)
