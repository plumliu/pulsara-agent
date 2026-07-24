from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pulsara_agent"


def _python_files(root: Path):
    return tuple(root.rglob("*.py"))


def _calls_named(path: Path, name: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == name
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        )
    ]


def test_only_committed_entry_builder_constructs_run_start() -> None:
    producers = {
        path.relative_to(SRC).as_posix(): _calls_named(path, "RunStartEvent")
        for path in _python_files(SRC)
    }
    producers = {path: lines for path, lines in producers.items() if lines}
    assert producers.keys() == {"runtime/run_entry.py"}


def test_primitives_never_import_runtime_or_host() -> None:
    violations: list[str] = []
    for path in _python_files(SRC / "primitives"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.startswith(
                    ("pulsara_agent.runtime", "pulsara_agent.host")
                ):
                    violations.append(f"{path.name}:{node.lineno}:{node.module}")
    assert violations == []


def test_host_stream_ingress_is_synchronous_owner_factory() -> None:
    path = SRC / "host" / "session.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name
        in {
            "stream_turn",
            "stream_approval_resolution",
            "stream_plan_interaction_resolution",
        }
    }
    assert set(methods) == {
        "stream_turn",
        "stream_approval_resolution",
        "stream_plan_interaction_resolution",
    }
    assert all(isinstance(node, ast.FunctionDef) for node in methods.values())


def test_agent_has_no_empty_basis_resume_fallback() -> None:
    source = (SRC / "runtime" / "agent.py").read_text(encoding="utf-8")
    assert "_exposure_from_state_or_resolve" not in source
    assert "_resolve_capability_exposure" not in source
    assert 'user_input=""' not in source
    assert "scratchpad[\"capability_exposure\"]" not in source
    assert "scratchpad.get(\"capability_exposure\")" not in source


def test_agent_accepts_only_committed_run_entries() -> None:
    path = SRC / "runtime" / "agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "run_task" not in method_names
    assert "stream_task" not in method_names
    assert "run_committed_entry" in method_names
    assert "stream_committed_entry" in method_names


def test_run_turn_waiter_is_detach_only_not_implicit_stop() -> None:
    source = (SRC / "host" / "session.py").read_text(encoding="utf-8")
    function = source[source.index("    async def _run_owned(") :]
    function = function[: function.index("    async def _stream_events_in_boundary_driver(")]
    assert "return await asyncio.shield(task)" in function
    assert "await task" not in function


def test_continuation_projection_intersection_uses_owned_identity_and_content() -> None:
    source = (SRC / "capability" / "facts.py").read_text(encoding="utf-8")
    function = source[source.index("def narrow_capability_projection_fact") :]
    assert "projection_entry_id" in function
    assert "content_fingerprint" in function
    assert "stable_name ==" not in function
    assert "fragment.fragment_artifact_id" in function


def test_segment_registry_owns_task_creation_after_owner_install() -> None:
    source = (SRC / "host" / "run_boundary.py").read_text(encoding="utf-8")
    function = source[source.index("    def install_segment(") :]
    function = function[: function.index("    def complete_segment(")]
    assert "driver_factory" in function
    assert "owner.active_segment = segment" in function
    assert "coroutine = driver_factory()" in function
    assert "asyncio.create_task(coroutine)" in function
    assert function.index("owner.active_segment = segment") < function.index(
        "coroutine = driver_factory()"
    )


def test_compaction_service_does_not_directly_append_event_log() -> None:
    service = (SRC / "runtime" / "compaction" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "event_log.append(" not in service
    assert "event_log.extend(" not in service


def test_capability_providers_have_no_mixed_turn_resolve_contract() -> None:
    forbidden_symbols = {
        "CapabilityResolveContext",
        "CapabilityProviderOutput",
        "resolve_for_turn",
    }
    violations: list[str] = []
    for path in _python_files(SRC / "capability"):
        source = path.read_text(encoding="utf-8")
        for symbol in forbidden_symbols:
            if symbol in source:
                violations.append(f"{path.relative_to(SRC)}:{symbol}")
    agent_source = (SRC / "runtime" / "agent.py").read_text(encoding="utf-8")
    for symbol in forbidden_symbols:
        if symbol in agent_source:
            violations.append(f"runtime/agent.py:{symbol}")
    assert violations == []


def test_run_start_user_truth_has_no_legacy_metadata_fallback() -> None:
    violations: list[str] = []
    for path in _python_files(SRC):
        source = path.read_text(encoding="utf-8")
        if 'metadata.get("user_input")' in source or "metadata.get('user_input')" in source:
            violations.append(str(path.relative_to(SRC)))
    assert violations == []


def test_permission_vocabulary_has_one_low_level_definition() -> None:
    definitions: list[str] = []
    preset_mappings: list[str] = []
    legacy_imports: list[str] = []
    for path in _python_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "PermissionMode":
                definitions.append(f"{relative}:{node.lineno}")
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                if any(
                    isinstance(target, ast.Name)
                    and target.id == "_PRESET_PERMISSION_PAYLOADS"
                    for target in targets
                ):
                    preset_mappings.append(f"{relative}:{node.lineno}")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "pulsara_agent.runtime.permission"
                and any(alias.name == "PermissionMode" for alias in node.names)
            ):
                legacy_imports.append(f"{relative}:{node.lineno}")
    assert definitions == ["primitives/permission.py:16"]
    assert len(preset_mappings) == 1
    assert preset_mappings[0].startswith("primitives/permission.py:")
    assert legacy_imports == []


def test_production_stop_reasons_are_not_free_strings() -> None:
    violations: list[str] = []
    for path in _python_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if any(
                    isinstance(target, ast.Attribute)
                    and target.attr == "stop_reason"
                    for target in node.targets
                ) and isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str
                ):
                    violations.append(
                        f"{path.relative_to(SRC)}:{node.lineno}:{node.value.value}"
                    )
    assert violations == []


def test_boundary_attempt_is_installed_before_activation_gate_opens() -> None:
    source = (SRC / "host" / "session.py").read_text(encoding="utf-8")
    function = source[source.index("    def _create_owned_boundary_task(") :]
    function = function[: function.index("    def _start_owned_boundary_stream(")]
    assert "attempt = HostRunBoundaryAttempt(" in function
    assert "self._boundary_attempt = attempt" in function
    assert "activation_gate.set()" in function
    assert function.index("self._boundary_attempt = attempt") < function.index(
        "activation_gate.set()"
    )


def test_boundary_facades_use_immutable_ingress_carriers() -> None:
    source = (SRC / "host" / "session.py").read_text(encoding="utf-8")
    assert "boundary_input = NewRunBoundaryInput(" in source
    assert "boundary_input = self._new_interaction_boundary_input(" in source
    assert "InteractionResumeBoundaryInput(" in source


def test_run_draft_builder_consumes_explicit_prepared_facts_not_scratchpad() -> None:
    source = (SRC / "runtime" / "run_entry.py").read_text(encoding="utf-8")
    function = source[source.index("async def prepare_agent_run_draft(") :]
    function = function[: function.index("\n\n__all__")]
    assert "scratchpad" not in function
    assert "repair_dangling_children" not in function
    assert "user_input: str" not in function
    assert "content=current_user_message.text" in function
    assert "id=current_user_message.message_id" in function
    for field in (
        "current_user_message",
        "terminal_run_end_event_id",
        "capability_basis",
        "new_run_boundary",
        "subagent_run_entry",
    ):
        assert field in function


def test_committed_run_uses_frozen_surface_and_child_borrow_path() -> None:
    agent = (SRC / "runtime" / "agent.py").read_text(encoding="utf-8")
    host = (SRC / "host" / "session.py").read_text(encoding="utf-8")
    assert "frozen_surface = draft.frozen_execution_surface" in agent
    assert 'state.scratchpad.get("frozen_capability_execution_surface")' not in agent
    assert "attempt.execution_handles = self._new_execution_handles(" in host
    assert "handles = attempt.execution_handles" in host
    assert "borrow_authority.borrow_child_tool_call()" in agent
    assert "child_execution_handles = BoundaryExecutionHandles(" in agent
    assert "subagent_runtime.attach_child_execution_handles(" in agent
    assert 'child_state.scratchpad["capability_execution_borrow_kind"] = "child"' in agent


def test_execution_handle_tracker_does_not_own_mcp_pending_interactions() -> None:
    source = (SRC / "runtime" / "execution_handles.py").read_text(encoding="utf-8")
    for forbidden in (
        "pending_mcp_interaction_leases",
        "promote_pending_mcp_lease",
        "complete_pending_mcp_lease",
    ):
        assert forbidden not in source

    supervisor = (SRC / "runtime" / "mcp" / "supervisor.py").read_text(
        encoding="utf-8"
    )
    for required in (
        "promote_lease_to_pending",
        "borrow_pending_lease",
        "return_pending_borrow",
        "complete_pending_lease",
    ):
        assert required in supervisor


def test_production_child_driver_consumes_prepared_subagent_entry() -> None:
    agent = (SRC / "runtime" / "agent.py").read_text(encoding="utf-8")
    driver = (SRC / "runtime" / "subagent" / "run_entry.py").read_text(
        encoding="utf-8"
    )
    assert "prepared_child_entry = PreparedSubagentRunEntry(" in agent
    assert "prepared=prepared_child_entry" in agent
    assert "prepared: PreparedSubagentRunEntry" in driver


def test_boundary_confirmation_retains_payload_not_only_ids() -> None:
    source = (SRC / "host" / "run_boundary.py").read_text(encoding="utf-8")
    assert "candidate_events: tuple[AgentEvent, ...]" in source
    assert "candidate_payload_fingerprints: tuple[str, ...]" in source


def test_compaction_cancel_path_has_no_unbounded_commit_task_await() -> None:
    source = (SRC / "runtime" / "compaction" / "commit.py").read_text(
        encoding="utf-8"
    )
    runtime_port = source[source.index("class RuntimeSessionCompactionEventCommitPort") :]
    assert "class DirectEventLogCompactionEventCommitPort" not in source
    assert "result = await task" not in runtime_port
    assert "confirm_event_batch(" not in runtime_port
    assert "PendingCompactionEventCommit(" in runtime_port
    assert "task=task" in runtime_port
