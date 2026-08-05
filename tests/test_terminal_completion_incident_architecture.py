from __future__ import annotations

import ast
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pulsara_agent"


def _source(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


def _tree(relative: str) -> ast.Module:
    return ast.parse(_source(relative), filename=relative)


def test_runtime_checkpoint_payload_owner_has_no_mutable_dict_annotation() -> None:
    tree = _tree("primitives/stored_event.py")
    checkpoint = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "RawRuntimeProjectionCheckpoint"
    )
    annotations = {
        node.target.id: ast.unparse(node.annotation)
        for node in checkpoint.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert annotations["validation_base_state"] == "CanonicalJsonObjectCarrier"
    assert annotations["state"] == "CanonicalJsonObjectCarrier"
    assert "dict" not in annotations["validation_base_state"]
    assert "dict" not in annotations["state"]


def test_terminal_semantic_reducers_have_no_checkpoint_or_storage_io() -> None:
    forbidden = {
        "write_runtime_projection_checkpoint",
        "read_runtime_projection_checkpoint",
        "postgres",
        "psycopg",
    }
    for relative in (
        "runtime/terminal/notification.py",
        "runtime/terminal/monitor.py",
    ):
        source = _source(relative)
        assert all(item not in source for item in forbidden), relative


def test_terminal_monitor_producers_use_typed_thread_settlement() -> None:
    source = _source("runtime/terminal/monitor.py")
    assert "settle_events_from_thread(" in source
    assert "write_events_from_thread(" not in source


def test_production_has_no_legacy_event_outcome_collapse_api() -> None:
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name == "emit_from_thread"
            ):
                violations.append(f"{path.relative_to(SRC)}:{node.lineno}:definition")
            if isinstance(node, ast.Attribute) and node.attr == "emit_from_thread":
                violations.append(f"{path.relative_to(SRC)}:{node.lineno}:access")
    for relative, class_name in (
        ("runtime/session.py", "RuntimeSession"),
        ("runtime/session_run_capabilities.py", "RuntimeSessionRunLedgerPort"),
    ):
        owner = next(
            node
            for node in _tree(relative).body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        for node in owner.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in {"emit", "emit_many"}
            ):
                violations.append(f"{relative}:{node.lineno}:{node.name}")
    assert violations == []


def test_agent_model_loop_has_repair_barriers_around_post_tool_projection() -> None:
    tree = _tree("runtime/agent.py")
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_stream_model_loop"
    )
    safe_points = tuple(
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "await_committed_reducer_repair_safe_point"
    )
    project_memory = next(
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_project_memory"
    )
    ingest_tool_results = next(
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_ingest_new_tool_result_projections"
    )
    assert any(line < project_memory for line in safe_points)
    assert any(line > ingest_tool_results for line in safe_points)


def test_governance_thread_adapter_uses_typed_batch_settlement() -> None:
    wiring = _source("runtime/wiring.py")
    assert "settle_events_from_thread(events).committed_events" in wiring
    assert "write_events_from_thread(events).committed_events" not in wiring


def test_checkpointed_terminal_fold_is_prepared_before_semantic_install() -> None:
    ingress = _source("runtime/projection_checkpoint_maintenance.py")
    session = _source("runtime/session.py")
    assert "PreparedCheckpointedCommittedReducerFold" in ingress
    assert "prepare_owned_events" in ingress
    assert "install_prepared_owned_events" in ingress
    assert "apply_owned_events_atomic" not in ingress
    apply_start = session.index("def _apply_live_receipt_to_reducer")
    apply_end = session.index("def _validate_committed_reducer_fold_result")
    apply_body = session[apply_start:apply_end]
    assert apply_body.index("_validate_committed_reducer_fold_result") < (
        apply_body.index("_commit_prepared_reducer_fold")
    )
    assert apply_body.index("_commit_prepared_reducer_fold") < apply_body.index(
        "registration.through_sequence = last"
    )


def test_unbounded_reducer_rebuild_is_offline_only() -> None:
    session = _source("runtime/session.py")
    assert "def _reconcile_committed_reducer_offline" in session
    assert "def reconcile_committed_reducer" not in session
    callers: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "_reconcile_committed_reducer_offline"
            ):
                callers.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert callers == []


def test_host_operational_projection_exposes_repair_owners() -> None:
    host = _source("host/session.py")
    runtime = _source("runtime/session.py")
    activation = _source("runtime/run_execution/service.py")
    assert "committed_reducer_operational_diagnostics" in runtime
    assert "run_finalization_diagnostics" in activation
    assert '"run_control_state": run_control_state' in host
    assert '"finalization": finalization_diagnostics' in host
    assert '"committed_reducers": reducer_diagnostics' in host


def test_terminal_physical_owner_consumes_only_narrow_thread_receipt() -> None:
    process = _source("runtime/terminal/process.py")
    tool_executor = _source("runtime/tool_executor.py")
    assert "RuntimeThreadEventSettlementReceipt" in process
    assert "EventWriteResult" not in process
    assert ".committed_events" not in process
    assert "RuntimeThreadEventSettlementReceipt" in tool_executor
    assert ".committed_events" not in tool_executor


def test_finalization_retry_is_classified_and_repair_receipt_driven() -> None:
    source = _source("runtime/run_execution/finalization.py")
    assert "waiting_reducer_repair" in source
    assert "wait_committed_reducer_repair" in source
    assert "resolved_write_outcome(exc)" in source
    assert "finalization reducer repair receipt is stale" in source
    assert "return asyncio.shield(task)" in source


def test_host_close_preserves_terminal_repair_dependency_order() -> None:
    source = _source("host/session.py")
    completion = source.index("terminal_sessions.drain_pending_completions")
    monitor_terminalization = source.index(
        "terminal_monitor_coordinator.terminate_all_for_session_close"
    )
    notification_terminalization = source.index("_close_terminal_notification_owners")
    finalization = source.index("drain_run_finalizations")
    compaction = source.index("drain_pending_terminalizations")
    subagents = source.index("cancel_active_children")
    mcp = source.index("mcp_tool_execution_port.stop_admission_and_drain")
    provider_input = source.index("quiesce_provider_input_event_producers_for_close")
    repair_barriers = tuple(
        match.start()
        for match in re.finditer("drain_open_committed_reducer_barrier", source)
    )
    repair_admission_close = source.index(
        "committed_reducer_repair_service.stop_admission_and_drain"
    )
    checkpoint = source.index(
        "runtime_projection_checkpoint_maintenance_service.stop_admission_and_drain"
    )
    assert len(repair_barriers) == 4
    assert completion < repair_barriers[0] < monitor_terminalization
    assert monitor_terminalization < repair_barriers[1] < notification_terminalization
    assert notification_terminalization < repair_barriers[2] < finalization
    assert finalization < provider_input < compaction < subagents < mcp
    assert mcp < repair_barriers[3] < repair_admission_close
    assert repair_admission_close < checkpoint


def test_resume_recovery_session_drains_checkpoint_owner_before_sync_close() -> None:
    resume = _source("host/resume.py")
    runtime = _source("runtime/session.py")

    repair_start = resume.index("async def repair_dangling_runs_for_resume")
    repair_end = resume.index("\ndef _can_defer_stateless_mcp_recovery", repair_start)
    repair_body = resume[repair_start:repair_end]
    assert "await runtime_session.teardown_temporary_recovery_session(" in repair_body
    assert "runtime_session.close()" not in repair_body

    teardown_start = runtime.index("async def teardown_temporary_recovery_session")
    teardown_end = runtime.index("\n    def close(self)", teardown_start)
    teardown = runtime[teardown_start:teardown_end]
    provider_input = teardown.index("quiesce_provider_input_event_producers_for_close")
    writer_repair_post_fold = teardown.index("drain_open_committed_reducer_barrier")
    checkpoint = teardown.index(
        "runtime_projection_checkpoint_maintenance_service.stop_admission_and_drain"
    )
    remaining_checkpoint = teardown.index(
        "transcript_projection_checkpoint_service.drain_pending"
    )
    final_io = teardown.index("context_input_io_service.drain_pending")
    sync_close = teardown.index("self.close()")

    assert (
        provider_input
        < writer_repair_post_fold
        < checkpoint
        < remaining_checkpoint
        < final_io
        < sync_close
    )

    production_callers: list[str] = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "teardown_temporary_recovery_session"
            ):
                production_callers.append(relative)
    assert production_callers == ["host/resume.py"]


def test_checkpoint_mutation_has_one_session_owned_production_owner() -> None:
    owners: list[str] = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write_runtime_projection_checkpoint"
            ):
                owners.append(relative)
    assert owners.count("runtime/projection_checkpoint_maintenance.py") == 1
    assert "runtime/session.py" not in owners


def test_accounted_admission_sees_final_physical_candidate_before_storage() -> None:
    account = _source("runtime/authority_materialization/account.py")
    session = _source("runtime/session.py")
    commit_start = account.index("def _commit_atomic")
    commit_end = account.index("def _require_state", commit_start)
    commit_body = account[commit_start:commit_end]

    assert "admission(candidates)" in commit_body
    assert commit_body.index("admission(candidates)") < commit_body.index(
        "extend_with_materialization_state"
    )
    assert "bind_pre_commit_admission(" in session
    assert "_assert_runtime_projection_physical_batch_admission" in session
