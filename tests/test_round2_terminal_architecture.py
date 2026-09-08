from __future__ import annotations

import asyncio
import ast
from dataclasses import fields
from pathlib import Path

import pytest
from pydantic import ValidationError

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_contracts import (
    ProcessLocalEffectSettlementToken,
)
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
    TERMINAL_MONITOR_TOOL_DESCRIPTION,
    TERMINAL_PROCESS_TOOL_DESCRIPTION,
    TERMINAL_TOOL_DESCRIPTION,
    parse_terminal_input,
    parse_terminal_monitor_input,
    parse_terminal_process_input,
    terminal_input_schema,
    terminal_monitor_input_schema,
    terminal_process_input_schema,
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
    SRC / "storage" / "migrations" / "sql" / "0000_conversation_kernel_baseline.sql"
)


def _repository_aggregate_source() -> str:
    kernel = SRC / "conversation_kernel"
    paths = [kernel / "repository.py"]
    paths.extend(sorted((kernel / "_repository").glob("*.py")))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _fixed_live_producers() -> dict[str, set[Path]]:
    result: dict[str, set[Path]] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                not isinstance(node.func, ast.Attribute)
                or node.func.attr != "offer_nowait"
            ):
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


def _schema_descriptions(value: object) -> tuple[str, ...]:
    descriptions: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            description = item.get("description")
            if isinstance(description, str):
                descriptions.append(description)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(descriptions)


def test_round2_closed_oracles_and_no_durable_terminal_authority(
    tmp_path: Path,
) -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 29
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 11
    assert len(APPEND_GUARDS) == 1
    assert len(CONVERSATION_KERNEL_RELATIONS) == 28
    terminal_names = {
        "terminal",
        "terminal_process",
        "terminal_monitor",
    }
    tool_owner = DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id="host:terminal-architecture",
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        session_id="session:terminal-architecture",
        live_bus=LiveAgentEventBus(),
    )
    try:
        execution_bindings = {
            item.tool_name: item for item in tool_owner.executor_bindings
        }
        assert terminal_names <= set(execution_bindings)
        for name in terminal_names:
            binding = execution_bindings[name]
            assert binding.catalog_entry is builtin_tool_catalog_entry(name)
            assert binding.executor_identity
    finally:
        asyncio.run(tool_owner.aclose())
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


def test_round2_terminal_observation_descriptor_and_payload_surface_are_narrow() -> (
    None
):
    descriptor = next(
        item
        for item in COMMITTED_EVENT_DESCRIPTORS
        if item.event_type is CommittedEventType.TERMINAL_OBSERVATION_ACCEPTED
    )
    assert descriptor.subject_slot is SubjectSlot.ENTRY
    assert descriptor.append_guards == (AppendGuardKind.HOST_WRITER,)
    source = _repository_aggregate_source()
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
        "prepared",
    ]
    prepared_field = fields(ProcessLocalEffectSettlementToken)[1]
    assert prepared_field.repr is False
    assert prepared_field.compare is False


def test_round2_live_terminal_events_have_only_real_process_local_producers() -> None:
    producers = _fixed_live_producers()
    assert producers["TERMINAL_PROCESS_COMPLETED"] == {
        Path("src/pulsara_agent/conversation_kernel/tool_runtime.py")
    }
    expected_monitor = {Path("src/pulsara_agent/terminal_process/monitor.py")}
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


def test_round2_three_terminal_prompts_teach_the_closed_lifecycle_roles() -> None:
    assert "does not stop or limit the command" in TERMINAL_TOOL_DESCRIPTION
    assert "status=running" in TERMINAL_TOOL_DESCRIPTION
    assert "copy its process_id exactly" in TERMINAL_TOOL_DESCRIPTION
    assert "In the main conversation" in TERMINAL_TOOL_DESCRIPTION
    assert "Prefer file tools for file reads and edits" in TERMINAL_TOOL_DESCRIPTION

    assert "Perform one immediate follow-up action" in TERMINAL_PROCESS_TOOL_DESCRIPTION
    assert "since_cursor may be copied" in TERMINAL_PROCESS_TOOL_DESCRIPTION
    assert "do not send another update later" in TERMINAL_PROCESS_TOOL_DESCRIPTION
    assert "Avoid repeated polling" in TERMINAL_PROCESS_TOOL_DESCRIPTION
    assert "environment closes or is replaced" in TERMINAL_PROCESS_TOOL_DESCRIPTION
    assert "wait once" not in TERMINAL_PROCESS_TOOL_DESCRIPTION

    assert TERMINAL_MONITOR_TOOL_DESCRIPTION.startswith(
        "Available only in the main conversation"
    )
    assert "only a completion update" in TERMINAL_MONITOR_TOOL_DESCRIPTION
    assert "may cause the agent to run again" in TERMINAL_MONITOR_TOOL_DESCRIPTION
    assert "do not poll merely to wait" in TERMINAL_MONITOR_TOOL_DESCRIPTION
    assert "does not stop the command" in TERMINAL_MONITOR_TOOL_DESCRIPTION
    assert "environment closes or is replaced" in TERMINAL_MONITOR_TOOL_DESCRIPTION

    terminal_properties = terminal_input_schema()["properties"]
    assert (
        "this is not a process_id"
        in terminal_properties["terminal_session_id"]["description"]
    )
    assert (
        "does not stop the command"
        in terminal_properties["yield_time_ms"]["description"]
    )

    process_input_schema = terminal_process_input_schema()
    process_descriptions = " ".join(_schema_descriptions(process_input_schema))
    for required_guidance in (
        "without adding a newline",
        "without the final newline",
        "without arranging a later update",
        "output_cursor copied unchanged",
    ):
        assert required_guidance in process_descriptions

    monitor_input_schema = terminal_monitor_input_schema()
    monitor_descriptions = " ".join(_schema_descriptions(monitor_input_schema))
    for required_guidance in (
        "receive only a completion update",
        "Completion updates remain enabled",
        "Closing or replacing the current terminal environment",
        "Controls output size",
    ):
        assert required_guidance in monitor_descriptions

    for schema in (process_input_schema, monitor_input_schema):
        for branch in schema["oneOf"]:
            assert "description" not in branch["properties"]["action"]

    public_prompt_surface = " ".join(
        (
            TERMINAL_TOOL_DESCRIPTION,
            TERMINAL_PROCESS_TOOL_DESCRIPTION,
            TERMINAL_MONITOR_TOOL_DESCRIPTION,
            *tuple(_schema_descriptions(terminal_input_schema())),
            *tuple(_schema_descriptions(process_input_schema)),
            *tuple(_schema_descriptions(monitor_input_schema)),
        )
    ).lower()
    for runtime_term in (
        "host-scoped",
        "host-local",
        "host replacement",
        "host close",
        "root-only",
        "root turn",
        "root agent",
        "toolresult",
        "future wake",
        "durable resume token",
        "physical completion",
        "physically join",
        "process group",
        "process identity",
        "cwd lane",
        "retention gap",
        "sanitized output",
        "accepted progress observation",
        "bounded policy",
    ):
        assert runtime_term not in public_prompt_surface


def test_round2_named_memory_and_process_bounds_are_exact() -> None:
    assert TERMINAL_RETAINED_OUTPUT_HARD_BYTES == 16 * 1024 * 1024
    assert TERMINAL_HOST_RETAINED_HARD_BYTES == 128 * 1024 * 1024
