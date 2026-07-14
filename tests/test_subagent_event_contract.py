from __future__ import annotations

import math

import pytest

from pydantic import ValidationError

from pulsara_agent.event import (
    EventContext,
    SubagentBudgetSnapshotEvent,
    SubagentCapabilityProfileSnapshotEvent,
    SubagentContextPolicySnapshotEvent,
    SubagentMessageSentEvent,
    SubagentResultConsumedEvent,
    SubagentResultDeliveredEvent,
    SubagentRunCompletedEvent,
    SubagentRunStartedEvent,
    SubagentTaskCompletedEvent,
    SubagentTaskCreatedEvent,
)
from pulsara_agent.event_log import dump_agent_event, load_agent_event
from pulsara_agent.event_log import AGENT_EVENT_SCHEMA_VERSION
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.long_horizon import default_child_rollout_policy
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.runtime.subagent import SubagentBudget


CTX = EventContext(run_id="run:subagent-contract", turn_id="turn:subagent-contract", reply_id="reply:subagent-contract")


def test_subagent_hard_cut_bumps_agent_event_schema_contract() -> None:
    assert AGENT_EVENT_SCHEMA_VERSION >= 2


def _context_snapshot() -> dict[str, object]:
    return {
        "mode": "isolated",
        "include_parent_summary": False,
        "include_parent_current_task": True,
        "include_parent_memory_projection": False,
        "include_parent_artifact_refs": False,
        "max_parent_context_chars": None,
        "fork_source_context_id": None,
    }


def _capability_snapshot() -> dict[str, object]:
    return {
        "profile_id": "subagent_capability_profile:contract",
        "profile_name": "general_worker",
        "inherited_from_parent_context_id": None,
        "permission_mode": PermissionMode.READ_ONLY.value,
        "permission_policy": preset_to_policy(PermissionMode.READ_ONLY).to_dict(),
        "allowed_tool_names": ["artifact_read", "report_agent_phase", "report_agent_result"],
        "allowed_descriptor_ids": [],
        "allowed_skill_names": [],
        "allowed_mcp_server_ids": [],
        "can_spawn_subagents": False,
        "max_spawn_depth_from_root": 0,
        "memory_enabled": False,
        "computed_from_parent_exposure_generation": None,
        "diagnostics": [],
    }


def _budget_snapshot(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "max_concurrent_children_per_parent_run": 4,
        "max_concurrent_children_per_host_session": 8,
        "max_spawn_depth_from_root": 0,
        "child_timeout_seconds": None,
        "max_total_child_runs_per_parent_run": 16,
        "max_result_summary_chars_per_child": 4_000,
        "max_result_artifact_refs_per_child": 32,
        "max_subagent_results_per_parent_compile": 8,
        "child_rollout_policy": default_child_rollout_policy().model_dump(
            mode="json"
        ),
    }
    value.update(overrides)
    return value


def _run_started_fields() -> dict[str, object]:
    return {
        **CTX.event_fields(),
        "subagent_run_id": "subagent_run:contract",
        "edge_id": "subagent_edge:contract:spawn",
        "parent_runtime_session_id": "runtime:parent",
        "parent_run_id": CTX.run_id,
        "child_runtime_session_id": "runtime:child",
        "role": "worker",
        "task_preview": "contract task",
        "context_policy": _context_snapshot(),
        "capability_profile": _capability_snapshot(),
    }


def test_subagent_run_started_requires_budget_snapshot() -> None:
    with pytest.raises(ValidationError):
        SubagentRunStartedEvent(**_run_started_fields())


def test_subagent_budget_snapshot_round_trip_is_immutable() -> None:
    event = SubagentRunStartedEvent(
        **_run_started_fields(),
        budget_snapshot=_budget_snapshot(),
    )
    loaded = load_agent_event(dump_agent_event(event))
    assert loaded == event
    assert isinstance(event.budget_snapshot, SubagentBudgetSnapshotEvent)
    assert SubagentBudget.from_event_snapshot(event.budget_snapshot).to_event_value() == (
        event.budget_snapshot.model_dump(mode="python")
    )
    with pytest.raises(ValidationError):
        event.budget_snapshot.max_concurrent_children_per_parent_run = 5


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_subagent_budget_snapshot_rejects_non_finite_timeout(timeout: float) -> None:
    with pytest.raises(ValidationError):
        SubagentBudgetSnapshotEvent.model_validate(
            _budget_snapshot(child_timeout_seconds=timeout)
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_concurrent_children_per_parent_run", 0),
        ("max_concurrent_children_per_host_session", 0),
        ("max_total_child_runs_per_parent_run", 0),
        ("max_subagent_results_per_parent_compile", 0),
        ("max_result_summary_chars_per_child", -1),
        ("max_result_artifact_refs_per_child", -1),
        ("max_spawn_depth_from_root", 1),
    ],
)
def test_subagent_budget_snapshot_rejects_invalid_caps(
    field_name: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        SubagentBudgetSnapshotEvent.model_validate(
            _budget_snapshot(**{field_name: value})
        )


def test_subagent_snapshot_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SubagentBudgetSnapshotEvent.model_validate(
            _budget_snapshot(unexpected=True)
        )


def test_subagent_capability_snapshot_requires_preset_permission_expansion() -> None:
    payload = _capability_snapshot()
    payload["permission_policy"] = preset_to_policy(PermissionMode.BYPASS_PERMISSIONS).to_dict()
    with pytest.raises(ValidationError):
        SubagentCapabilityProfileSnapshotEvent.model_validate(payload)


def test_subagent_context_snapshot_rejects_invalid_fork_contract() -> None:
    payload = _context_snapshot()
    payload["fork_source_context_id"] = "context:fork"
    with pytest.raises(ValidationError):
        SubagentContextPolicySnapshotEvent.model_validate(payload)
    fork_payload = _context_snapshot()
    fork_payload["mode"] = "fork"
    with pytest.raises(ValidationError):
        SubagentContextPolicySnapshotEvent.model_validate(fork_payload)


def test_subagent_event_serialization_preserves_all_snapshots() -> None:
    event = SubagentRunStartedEvent(
        **_run_started_fields(),
        budget_snapshot=_budget_snapshot(child_timeout_seconds=12.5),
    )
    loaded = load_agent_event(dump_agent_event(event))
    assert isinstance(loaded, SubagentRunStartedEvent)
    assert loaded.context_policy == event.context_policy
    assert loaded.capability_profile == event.capability_profile
    assert loaded.budget_snapshot == event.budget_snapshot


def test_subagent_task_created_requires_objective_artifact() -> None:
    with pytest.raises(ValidationError):
        SubagentTaskCreatedEvent(
            **CTX.event_fields(),
            task_id="task:contract",
            profile_id="general_worker",
            objective_preview="task",
        )


def test_subagent_message_sent_requires_message_artifact() -> None:
    with pytest.raises(ValidationError):
        SubagentMessageSentEvent(
            **CTX.event_fields(),
            edge_id="edge:contract",
            subagent_run_id="run:contract",
            parent_runtime_session_id="runtime:parent",
            parent_run_id=CTX.run_id,
            child_runtime_session_id="runtime:child",
            message_preview="task",
            delivery_kind="spawn_task",
        )


def test_subagent_completion_requires_run_result_and_artifact() -> None:
    with pytest.raises(ValidationError):
        SubagentRunCompletedEvent(
            **CTX.event_fields(),
            subagent_run_id="run:contract",
            parent_runtime_session_id="runtime:parent",
            child_runtime_session_id="runtime:child",
            result_id="result:contract",
            summary="done",
            result_artifact_id="artifact:result",
            artifact_ids=[],
        )
    with pytest.raises(ValidationError):
        SubagentTaskCompletedEvent(
            **CTX.event_fields(),
            task_id="task:contract",
        )


def test_subagent_result_consumed_enforces_kind_target_and_terminal_invariants() -> None:
    fields = {
        **CTX.event_fields(),
        "consumption_id": "consumption:contract",
        "consumer_tool_call_id": "tool:wait",
        "kind": "wait_task",
        "task_id": "task:contract",
        "consumed_status": "failed",
    }
    with pytest.raises(ValidationError):
        SubagentResultConsumedEvent(**fields)
    completed_fields = {
        **fields,
        "consumed_status": "completed",
        "terminal_event_id": "event:terminal",
    }
    with pytest.raises(ValidationError):
        SubagentResultConsumedEvent(**completed_fields)


def test_subagent_result_delivered_requires_model_call_join_fields() -> None:
    with pytest.raises(ValidationError):
        SubagentResultDeliveredEvent(
            **CTX.event_fields(),
            subagent_run_id="run:contract",
            parent_runtime_session_id="runtime:parent",
            result_id="result:contract",
            summary="done",
        )
