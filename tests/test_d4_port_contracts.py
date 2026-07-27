from __future__ import annotations

from dataclasses import replace
import pickle

import pytest
from pydantic import ValidationError

from pulsara_agent.event import EventContext
from pulsara_agent.ports.projection_jobs import (
    build_canonical_mutation_transaction_identity,
    build_memory_uow_physical_transaction_request,
    build_projection_migration_readiness_view,
    build_projection_migration_transaction_identity,
)
from pulsara_agent.ports.subagent import (
    SpawnAgentCommand,
    build_subagent_command_owner,
    build_subagent_tool_command,
)
from pulsara_agent.ports.terminal import (
    TerminalMonitorRegisterInput,
    TerminalProcessWaitInput,
    parse_terminal_monitor_input,
    parse_terminal_process_input,
)
from pulsara_agent.ports.tool_execution import (
    ToolCall,
    ToolInvocationOwnerKind,
    ToolPermissionInvocation,
)
from pulsara_agent.ports.tool_registry import (
    BuiltinToolBindingContract,
    ToolBindingOrigin,
    build_tool_binding_contract,
)
from pulsara_agent.primitives.permission import PermissionMode


def _permission() -> ToolPermissionInvocation:
    return ToolPermissionInvocation(
        permission_snapshot_id="permission:test",
        permission_mode=PermissionMode.BYPASS_PERMISSIONS,
        permission_policy_fingerprint="sha256:policy",
        terminal_access="allow",
        network_isolated=False,
        source_run_permission_snapshot_fingerprint="sha256:snapshot",
    )


def test_tool_call_recursively_freezes_model_owned_json() -> None:
    source = {"nested": {"items": [1, {"enabled": True}]}}
    call = ToolCall(id="call:test", name="demo", arguments=source)

    source["nested"]["items"][1]["enabled"] = False  # type: ignore[index]

    assert call.arguments["nested"]["items"][1]["enabled"] is True  # type: ignore[index]
    with pytest.raises(TypeError):
        call.arguments["new"] = "value"


def test_terminal_closed_inputs_are_strict_and_discriminated() -> None:
    process = parse_terminal_process_input(
        {
            "action": "wait",
            "process_id": "process:test",
            "timeout_seconds": 30,
        }
    )
    monitor = parse_terminal_monitor_input(
        {"action": "register", "process_id": "process:test"}
    )

    assert isinstance(process, TerminalProcessWaitInput)
    assert isinstance(monitor, TerminalMonitorRegisterInput)
    with pytest.raises(ValidationError):
        parse_terminal_process_input(
            {"action": "wait", "process_id": "process:test", "extra": True}
        )
    with pytest.raises(ValidationError):
        parse_terminal_monitor_input(
            {"action": "register", "process_id": "process:test", "extra": True}
        )


def test_subagent_command_factory_owns_closed_validation_and_fingerprint() -> None:
    owner = build_subagent_command_owner(
        runtime_session_id="runtime:test",
        tool_call_id="call:spawn",
        tool_name="spawn_agent",
        event_context=EventContext(
            run_id="run:test", turn_id="turn:test", reply_id="reply:test"
        ),
        parent_context_id="context:test",
        parent_model_call_index=1,
        invocation_owner_kind=ToolInvocationOwnerKind.HOST_MAIN_RUN,
        permission=_permission(),
        bound_child_subagent_run_id=None,
    )
    command = build_subagent_tool_command(
        owner=owner,
        arguments={"task": "inspect", "role": "verifier", "context": "fork"},
    )

    assert isinstance(command, SpawnAgentCommand)
    assert command.role == "verifier"
    assert command.context_mode == "fork"
    assert command.command_fingerprint.startswith("sha256:")
    with pytest.raises(ValueError, match="unexpected fields"):
        build_subagent_tool_command(
            owner=owner,
            arguments={"task": "inspect", "unexpected": True},
        )


def test_binding_union_factory_rejects_incomplete_mcp_identity() -> None:
    builtin = build_tool_binding_contract(
        tool_name="terminal",
        origin=ToolBindingOrigin.BUILTIN,
        contract_id="builtin.terminal",
        contract_version="v1",
    )
    assert isinstance(builtin, BuiltinToolBindingContract)
    assert builtin.binding_fingerprint.startswith("sha256:")

    with pytest.raises(ValueError, match="exact identity"):
        build_tool_binding_contract(
            tool_name="mcp__docs__lookup",
            origin=ToolBindingOrigin.MCP,
            contract_id="mcp.docs.lookup",
            contract_version="v1",
            original_tool_name="lookup",
        )


def test_projection_factories_recompute_fingerprints_and_reject_drift() -> None:
    transaction = build_canonical_mutation_transaction_identity(
        schema_binding_fingerprint="sha256:schema",
        connection_provider_borrower_id="borrower:test",
        transaction_owner_id="uow:test",
        transaction_generation=1,
        backend_pid=42,
        admission_epoch_fingerprint="sha256:epoch",
        admission_guard_lock_identity_fingerprint="sha256:guard",
    )
    request = build_memory_uow_physical_transaction_request(
        transaction_owner_id="uow:test",
        transaction_generation=1,
        deadline_monotonic=100.0,
        scope_request_fingerprint="sha256:scope",
    )
    migration = build_projection_migration_transaction_identity(
        database_target_fingerprint="sha256:database",
        database_oid=10,
        backend_pid=42,
        current_head_version=5,
        current_registry_prefix_fingerprint="sha256:registry",
        maintenance_operation_id="maintenance:test",
        maintenance_epoch_fingerprint="sha256:maintenance",
        transaction_generation=1,
    )
    readiness = build_projection_migration_readiness_view(
        legacy_surface_binding_plan_ready=True,
        timeline_coverage_ready=True,
        evidence_coverage_ready=False,
    )

    assert transaction.identity_fingerprint.startswith("sha256:")
    assert request.request_fingerprint.startswith("sha256:")
    assert migration.identity_fingerprint.startswith("sha256:")
    assert readiness.authority_fingerprint.startswith("sha256:")
    with pytest.raises(ValueError, match="drifted"):
        replace(transaction, backend_pid=43)
    with pytest.raises(ValueError, match="mismatch"):
        replace(readiness, evidence_coverage_ready=True)


def test_projection_authority_carriers_are_not_serializable() -> None:
    from pulsara_agent.ports.projection_jobs import (
        issue_canonical_mutation_driver_authority,
        issue_memory_uow_scope_factory_authority,
        issue_projection_migration_port_authority,
    )

    for authority in (
        issue_canonical_mutation_driver_authority(),
        issue_memory_uow_scope_factory_authority(),
        issue_projection_migration_port_authority(),
    ):
        with pytest.raises(TypeError):
            pickle.dumps(authority)
