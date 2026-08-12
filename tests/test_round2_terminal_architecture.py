from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

import pytest
from pydantic import ValidationError

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.job_catalog import JOB_HANDLER_CATALOG
from pulsara_agent.conversation_kernel.runner import (
    ProcessLocalEffectSettlementToken,
)
from pulsara_agent.conversation_kernel.tool_runtime import DIRECT_KERNEL_TOOL_NAMES
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
    AppendGuardKind,
    CommittedEventType,
    SubjectSlot,
)
from pulsara_agent.ports.terminal import (
    parse_terminal_input,
    parse_terminal_monitor_input,
    parse_terminal_process_input,
)
from pulsara_agent.ports.terminal_observation import (
    TerminalObservationInstallationAttempt,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.terminal_process.output import (
    TERMINAL_HOST_RETAINED_HARD_BYTES,
    TERMINAL_RETAINED_OUTPUT_HARD_BYTES,
)


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pulsara_agent"
BASELINE = (
    SRC
    / "storage"
    / "migrations"
    / "sql"
    / "0000_conversation_kernel_baseline.sql"
)


def _fixed_live_producers() -> dict[str, set[Path]]:
    result: dict[str, set[Path]] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "offer_nowait":
                continue
            keyword = next(
                (item for item in node.keywords if item.arg == "event_type"), None
            )
            if (
                keyword is None
                or not isinstance(keyword.value, ast.Attribute)
                or not isinstance(keyword.value.value, ast.Name)
                or keyword.value.value.id != "LiveEventType"
            ):
                continue
            result.setdefault(keyword.value.attr, set()).add(path.relative_to(ROOT))
    return result


def test_round2_closed_oracles_and_no_durable_terminal_authority() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 34
    assert len(LIVE_EVENT_TYPES) == 23
    assert len(SUBJECT_SLOTS) == 15
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 26
    assert len(JOB_HANDLER_CATALOG) == 4
    terminal_names = {
        "terminal",
        "terminal_process",
        "terminal_monitor",
    }
    assert terminal_names <= DIRECT_KERNEL_TOOL_NAMES
    for name in terminal_names:
        assert builtin_tool_catalog_entry(name).descriptor.name == name
    assert not any(
        any(token in relation for token in ("terminal", "monitor", "notification"))
        for relation in CONVERSATION_KERNEL_RELATIONS
    )
    baseline = BASELINE.read_text(encoding="utf-8")
    for forbidden in (
        "terminal_processes",
        "terminal_outputs",
        "terminal_monitors",
        "terminal_notifications",
        "terminal_delivery_receipts",
    ):
        assert forbidden not in baseline


def test_round2_terminal_observation_descriptor_and_payload_surface_are_narrow() -> None:
    descriptor = next(
        item
        for item in COMMITTED_EVENT_DESCRIPTORS
        if item.event_type is CommittedEventType.TERMINAL_OBSERVATION_ACCEPTED
    )
    assert descriptor.subject_slot is SubjectSlot.ENTRY
    assert descriptor.append_guards == (AppendGuardKind.HOST_WRITER,)
    source = (SRC / "conversation_kernel" / "repository.py").read_text(
        encoding="utf-8"
    )
    event_method = source[
        source.index("def _terminal_observation_event(") : source.index(
            "def _insert_entry(", source.index("def _terminal_observation_event(")
        )
    ]
    for forbidden in (
        '"output"',
        '"cursor"',
        '"policy"',
        '"callback"',
        '"lease"',
        '"pid"',
        '"raw"',
    ):
        assert forbidden not in event_method
    assert TerminalObservationInstallationAttempt.__dataclass_params__.frozen is True
    assert [item.name for item in fields(ProcessLocalEffectSettlementToken)] == [
        "token_id",
        "token_fingerprint",
    ]


def test_round2_live_terminal_events_have_only_real_process_local_producers() -> None:
    producers = _fixed_live_producers()
    assert producers["TERMINAL_PROCESS_COMPLETED"] == {
        Path("src/pulsara_agent/conversation_kernel/tool_runtime.py")
    }
    expected_monitor = {
        Path("src/pulsara_agent/terminal_process/monitor.py")
    }
    for name in (
        "TERMINAL_MONITOR_OPENED",
        "TERMINAL_MONITOR_OBSERVATION",
        "TERMINAL_MONITOR_CLOSED",
    ):
        assert producers[name] == expected_monitor
    assert "_offer_terminal_live" not in "\n".join(
        path.read_text(encoding="utf-8") for path in SRC.rglob("*.py")
    )


def test_round2_physical_and_monitor_owners_do_not_import_semantic_authority() -> None:
    output = (SRC / "terminal_process" / "output.py").read_text(encoding="utf-8")
    manager = (SRC / "terminal_process" / "manager.py").read_text(encoding="utf-8")
    monitor = (SRC / "terminal_process" / "monitor.py").read_text(encoding="utf-8")
    for source in (output, manager):
        for forbidden in (
            "conversation_kernel.repository",
            "terminal_protocol",
            "storage.migrations",
            "agent_events",
            "EventLog",
        ):
            assert forbidden not in source
    for forbidden in (
        "conversation_kernel.repository",
        "conversation_kernel.runner",
        "storage.migrations",
        "conversation_kernel.jobs",
        "EventLog",
    ):
        assert forbidden not in monitor
    assert "pickle" not in monitor
    assert "sqlite" not in monitor
    assert "open(" not in monitor


def test_round2_cursor_and_initial_entry_contract_are_not_dual_sources() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    assert "terminal_cursor" not in baseline
    assert "retained_from_cursor" not in baseline
    assert "through_cursor" not in baseline
    assert "initial_entry_id text NOT NULL" in baseline
    assert "DEFERRABLE INITIALLY DEFERRED" in baseline
    assert "enforce_conversation_kernel_invariants" in baseline
    assert "initial_entry_kind" not in baseline


def test_round2_strict_three_tool_input_contracts_fail_closed() -> None:
    assert parse_terminal_input({"command": "true"}).yield_time_ms == 10_000
    with pytest.raises(ValidationError):
        parse_terminal_input({"command": "true", "yield_time_ms": "1"})
    with pytest.raises(ValidationError):
        parse_terminal_input({"command": "true", "unexpected": True})
    with pytest.raises(ValidationError):
        parse_terminal_process_input(
            {"action": "wait", "process_id": "p", "timeout_seconds": "1"}
        )
    with pytest.raises(ValidationError):
        parse_terminal_monitor_input(
            {"action": "cancel", "monitor_id": "m", "extra": "open"}
        )


def test_round2_named_memory_and_process_bounds_are_exact() -> None:
    assert TERMINAL_RETAINED_OUTPUT_HARD_BYTES == 16 * 1024 * 1024
    assert TERMINAL_HOST_RETAINED_HARD_BYTES == 128 * 1024 * 1024
