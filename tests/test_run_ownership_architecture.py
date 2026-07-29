from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pulsara_agent"


def _tree(relative: str) -> ast.Module:
    path = SRC / relative
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _annotated_fields(class_node: ast.ClassDef) -> set[str]:
    return {
        node.target.id
        for node in class_node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def _imported_modules(tree: ast.AST) -> tuple[str, ...]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return tuple(modules)


def test_host_session_consumes_only_opaque_run_control_contracts() -> None:
    tree = _tree("host/session.py")
    imported = _imported_modules(tree)
    assert "pulsara_agent.runtime.run_execution.owner" not in imported
    assert "pulsara_agent.runtime.run_execution.registry" not in imported
    forbidden_names = {
        "LoopState",
        "RunActivationWorkingState",
        "RunExecutionRegistry",
        "RunOwner",
    }
    assert not {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in forbidden_names
    }
    forbidden_attributes = {
        "_active_state",
        "_suspended_state",
        "_preparing_state",
        "_run_execution_owners",
    }
    assert not {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes
    }


def test_public_agent_result_and_activation_cache_do_not_expose_legacy_state() -> None:
    result = _class(_tree("runtime/agent.py"), "AgentRunResult")
    assert "state" not in _annotated_fields(result)

    working_state = _class(_tree("runtime/state.py"), "RunActivationWorkingState")
    assert "scratchpad" not in _annotated_fields(working_state)
    assert all(
        not (isinstance(node, ast.ClassDef) and node.name == "LoopState")
        for path in SRC.rglob("*.py")
        for node in ast.walk(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    )


def test_production_has_no_scratchpad_access_or_renamed_fallback() -> None:
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "scratchpad":
                violations.append(f"{relative}:{node.lineno}:attribute")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == "scratchpad"
            ):
                violations.append(f"{relative}:{node.lineno}:literal")
    assert violations == []


def test_run_registry_is_composition_owned_not_host_or_runtime_session_owned() -> None:
    for relative in ("host/session.py", "runtime/session.py"):
        tree = _tree(relative)
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RunExecutionRegistry"
            for node in ast.walk(tree)
        )
        assert "pulsara_agent.runtime.run_execution.registry" not in _imported_modules(
            tree
        )

    wiring = _tree("runtime/wiring.py")
    factory = _tree("runtime/run_execution/factory.py")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RunActivationFactory"
        for node in ast.walk(wiring)
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RunExecutionRegistry"
        for node in ast.walk(factory)
    )


def test_common_factory_is_the_only_production_agent_runtime_constructor() -> None:
    constructors: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AgentRuntime"
            ):
                constructors.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno}")
    assert len(constructors) == 1
    assert constructors[0].startswith("runtime/run_execution/factory.py:")

    child = (SRC / "runtime/subagent/activation.py").read_text(encoding="utf-8")
    assert "self._activation_factory.create(" in child
    assert "AgentRuntime(" not in child
    assert "_run_child_agent" not in child


def test_agent_runtime_has_no_complete_runtime_session_binding() -> None:
    tree = _tree("runtime/agent.py")
    agent = _class(tree, "AgentRuntime")
    init = next(
        node
        for node in agent.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    argument_names = {
        argument.arg for argument in (*init.args.args, *init.args.kwonlyargs)
    }
    assert "runtime_session" not in argument_names
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == "runtime_session"
        for node in ast.walk(agent)
    )


def test_child_admission_owner_does_not_own_run_execution() -> None:
    tree = _tree("runtime/subagent/execution.py")
    owner = _class(tree, "ChildAdmissionSessionOwner")
    fields = _annotated_fields(owner)
    assert fields.isdisjoint(
        {
            "coroutine",
            "task",
            "execution_handles",
            "finalization",
            "run_completion",
        }
    )
    registry = _class(tree, "ChildAdmissionSessionRegistry")
    method_names = {
        node.name
        for node in registry.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "attach_coroutine" not in method_names
    assert "attach_execution_handles" not in method_names


def test_run_finalization_owner_is_the_only_terminal_state_owner() -> None:
    tree = _tree("runtime/run_execution/owner.py")
    run_owner = _class(tree, "RunOwner")
    run_owner_fields = _annotated_fields(run_owner)
    assert "terminal_state" not in run_owner_fields
    assert "terminal_candidate" not in run_owner_fields
    assert "terminal_event_id" not in run_owner_fields
    assert "terminalization_task" not in run_owner_fields
    assert not {
        node.name
        for node in run_owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"terminal_state", "terminal_candidate"}
    }

    finalization_owner = _class(tree, "RunFinalizationOwner")
    assert {
        "commit_state",
        "terminal_event_id",
        "physical_task",
        "run_end_candidate",
        "confirmed_run_end_event_reference",
    }.issubset(_annotated_fields(finalization_owner))


def test_run_execution_generic_event_candidate_allowlist_is_exact() -> None:
    violations: list[str] = []
    forbidden_names = {
        "EventLogTransactionCompanion",
        "FrozenEventWriteCandidate",
        "freeze_event_write_candidate",
    }
    package = SRC / "runtime/run_execution"
    for path in package.rglob("*.py"):
        if path.name == "commit_gateway.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        used = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id in forbidden_names
        }
        if used:
            violations.append(
                f"{path.relative_to(SRC).as_posix()}:{','.join(sorted(used))}"
            )
    assert violations == []


def test_run_execution_does_not_import_runtime_session() -> None:
    violations: list[str] = []
    for path in (SRC / "runtime/run_execution").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if module == "pulsara_agent.runtime.session":
                violations.append(f"{path.relative_to(SRC).as_posix()}:{module}")
    assert violations == []


def test_closed_activation_outcome_is_the_only_production_waiter_contract() -> None:
    service = (SRC / "runtime/run_execution/service.py").read_text(encoding="utf-8")
    host = (SRC / "host/session.py").read_text(encoding="utf-8")
    child = (SRC / "runtime/subagent/activation.py").read_text(encoding="utf-8")

    assert "wait_public_result" not in service
    assert "public_completion" not in service
    assert "result_from_owned_state" not in service
    assert "borrow_run_completion" not in service
    assert "wait_public_result" not in host
    assert "borrow_run_completion" not in host
    assert "result_from_owned_state" not in host
    assert "dispatch.wait_activation()" in host
    assert "dispatch.wait_activation()" in child


def test_pending_interaction_rebind_has_no_synchronous_event_log_read() -> None:
    interaction = (SRC / "runtime/run_execution/interaction.py").read_text(
        encoding="utf-8"
    )

    assert ".get_by_id(" not in interaction
    assert "EventLog" not in interaction
    assert "rebind_confirmed_event_candidate" in interaction
