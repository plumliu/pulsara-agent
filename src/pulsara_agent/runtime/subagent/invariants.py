"""Shared pure invariants for subagent reducer and pre-commit planning."""

from __future__ import annotations


def run_attribution_error(
    *,
    expected_parent_runtime_session_id: str,
    expected_child_runtime_session_id: str,
    expected_reported_child_run_id: str | None,
    parent_runtime_session_id: str | None = None,
    child_runtime_session_id: str | None = None,
    reported_child_run_id: str | None = None,
) -> str | None:
    """Return a stable diagnostic code when an event drifts from its run."""

    if (
        parent_runtime_session_id is not None
        and parent_runtime_session_id != expected_parent_runtime_session_id
    ):
        return "subagent_task_run_attribution_mismatch"
    if (
        child_runtime_session_id is not None
        and child_runtime_session_id != expected_child_runtime_session_id
    ):
        return "subagent_task_run_attribution_mismatch"
    if (
        reported_child_run_id is not None
        and expected_reported_child_run_id not in {None, reported_child_run_id}
    ):
        return "child_run_attribution_mismatch"
    return None


def creation_attribution_matches(
    *,
    expected_batch_id: str | None,
    expected_create_tool_call_id: str | None,
    batch_id: str | None,
    create_tool_call_id: str | None,
) -> bool:
    return (
        expected_batch_id == batch_id
        and expected_create_tool_call_id == create_tool_call_id
    )
