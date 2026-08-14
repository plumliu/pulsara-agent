"""Round 4 Plan/permission authority and subtraction architecture gates."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from pulsara_agent.conversation_kernel.job_catalog import JOB_HANDLER_CATALOG
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
    AppendGuardKind,
    CommittedEventType,
    SubjectSlot,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/pulsara_agent"
KERNEL = SOURCE / "conversation_kernel"
BASELINE = (
    SOURCE
    / "storage/migrations/sql/0000_conversation_kernel_baseline.sql"
)

PLAN_DESCRIPTORS = {
    CommittedEventType.PLAN_WORKFLOW_ENTERED: SubjectSlot.PLAN_WORKFLOW,
    CommittedEventType.PLAN_QUESTION_ASKED: SubjectSlot.PLAN_INTERACTION,
    CommittedEventType.PLAN_QUESTION_ANSWERED: SubjectSlot.PLAN_INTERACTION,
    CommittedEventType.PLAN_DRAFT_SUBMITTED: SubjectSlot.PLAN_INTERACTION,
    CommittedEventType.PLAN_DRAFT_DECISION_ACCEPTED: SubjectSlot.PLAN_INTERACTION,
    CommittedEventType.PLAN_WORKFLOW_EXITED: SubjectSlot.PLAN_WORKFLOW,
    CommittedEventType.PLAN_CONTINUATION_ACCEPTED: SubjectSlot.ENTRY,
}


def _repository_aggregate_source() -> str:
    paths = [KERNEL / "repository.py"]
    paths.extend(sorted((KERNEL / "_repository").glob("*.py")))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_round4_final_oracles_and_plan_descriptors_are_exact() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 34
    assert len(LIVE_EVENT_TYPES) == 23
    assert len(SUBJECT_SLOTS) == 15
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 26
    assert len(JOB_HANDLER_CATALOG) == 4

    observed = {
        descriptor.event_type: descriptor
        for descriptor in COMMITTED_EVENT_DESCRIPTORS
        if descriptor.event_type in PLAN_DESCRIPTORS
    }
    assert set(observed) == set(PLAN_DESCRIPTORS)
    for event_type, subject_slot in PLAN_DESCRIPTORS.items():
        descriptor = observed[event_type]
        assert descriptor.subject_slot is subject_slot
        assert descriptor.append_guards == (AppendGuardKind.HOST_WRITER,)


def test_round4_schema_has_exact_plan_relations_and_required_initial_entry() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")
    assert baseline.count("CREATE TABLE pulsara_v3.") == 26
    assert set(CONVERSATION_KERNEL_RELATIONS) >= {
        "plan_workflows",
        "plan_interactions",
    }
    turns = baseline.split("CREATE TABLE pulsara_v3.turns (", 1)[1].split(
        "CREATE TABLE ", 1
    )[0]
    assert "initial_entry_id text NOT NULL" in turns
    assert "permission_snapshot_id text NOT NULL" in turns
    assert "permission_snapshot_fingerprint text NOT NULL" in turns
    assert "CONSTRAINT ck_turn_permission_overlay_exact CHECK" in turns
    queue = baseline.split(
        "CREATE TABLE pulsara_v3.prompt_queue_items (", 1
    )[1].split("CREATE TABLE ", 1)[0]
    assert "CONSTRAINT ck_prompt_queue_permission_overlay_exact CHECK" in queue
    assert "effective_permission_mode = requested_permission_mode" in turns
    assert "effective_permission_mode = requested_permission_mode" in queue
    assert (
        "CREATE CONSTRAINT TRIGGER trg_pulsara_v3_turn_initial_entry_integrity"
        in baseline
    )
    assert "DEFERRABLE INITIALLY DEFERRED" in baseline
    assert "CREATE UNIQUE INDEX uq_pulsara_v3_active_plan_workflow" in baseline
    assert "CREATE UNIQUE INDEX uq_pulsara_v3_open_plan_interaction" in baseline


def test_round4_every_turn_creator_installs_frozen_permission_columns() -> None:
    source = _repository_aggregate_source()
    turn_inserts = tuple(
        match.group(1)
        for match in re.finditer(
            r"INSERT INTO pulsara_v3\.turns\s*\((.*?)\)\s*VALUES",
            source,
            flags=re.DOTALL,
        )
    )
    assert len(turn_inserts) == 7
    required = {
        "initial_entry_id",
        "permission_snapshot_id",
        "requested_permission_mode",
        "effective_permission_mode",
        "permission_admission_source",
        "permission_overlay",
        "permission_plan_context_ordinal",
        "permission_contract_id",
        "permission_contract_fingerprint",
        "permission_snapshot_fingerprint",
    }
    for columns in turn_inserts:
        observed = {item.strip() for item in columns.split(",")}
        assert required <= observed

    queue_inserts = tuple(
        match.group(1)
        for match in re.finditer(
            r"INSERT INTO pulsara_v3\.prompt_queue_items\s*\((.*?)\)\s*VALUES",
            source,
            flags=re.DOTALL,
        )
    )
    assert len(queue_inserts) == 1
    assert {
        "permission_snapshot_id",
        "requested_permission_mode",
        "effective_permission_mode",
        "permission_snapshot_fingerprint",
    } <= {item.strip() for item in queue_inserts[0].split(",")}


def test_round4_compiler_has_no_repository_or_runtime_authority() -> None:
    pure_modules = (
        SOURCE / "model_input/contracts.py",
        SOURCE / "model_input/compiler.py",
        SOURCE / "model_input/lowering.py",
        KERNEL / "context_sources.py",
    )
    forbidden_roots = (
        "pulsara_agent.conversation_kernel.repository",
        "pulsara_agent.storage",
        "psycopg",
    )
    for path in pure_modules:
        imports = _imports(path)
        assert not any(
            imported == root or imported.startswith(root + ".")
            for imported in imports
            for root in forbidden_roots
        ), path


def test_round4_has_no_plan_recovery_job_or_extra_guard() -> None:
    assert not any("PLAN" in item.handler_type for item in JOB_HANDLER_CATALOG)
    assert APPEND_GUARDS == ("HostWriterGuard", "JobAttemptClaimGuard")
    production = tuple(sorted(KERNEL.glob("*.py"))) + tuple(
        sorted((SOURCE / "model_input").glob("*.py"))
    )
    forbidden = (
        "PlanSnapshotEvent",
        "PlanCheckpoint",
        "PlanRecovery",
        "PlanReceipt",
        "PlanReducer",
        "DurablePlanRunner",
    )
    hits = [
        f"{path.relative_to(ROOT)}:{token}"
        for path in production
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_round4_continuation_physical_owner_is_host_local_only() -> None:
    host = (KERNEL / "host.py").read_text(encoding="utf-8")
    runner = (KERNEL / "runner.py").read_text(encoding="utf-8")
    gateway = (SOURCE / "terminal_protocol/v3_gateway.py").read_text(
        encoding="utf-8"
    )
    assert "ContinuationAdmissionOwner()" in host
    assert "self._plan_continuations.start(" in host
    assert "_accept_automatic_plan_continuation" in host
    assert "continuation_owner.start(" not in runner
    assert "ContinuationAdmissionOwner" not in gateway


def test_round4_all_root_producers_use_the_single_host_owned_chain() -> None:
    path = KERNEL / "host.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    direct_runner_callers: set[str] = set()
    root_chain_callers: set[str] = set()
    for name, function in functions.items():
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "run_accepted_turn":
                direct_runner_callers.add(name)
            elif node.func.attr == "_run_accepted_root_chain":
                root_chain_callers.add(name)

    assert direct_runner_callers == {
        "_finish_root_chain",
        "_run_accepted_root_chain",
    }
    assert {
        "_bind_plan_review_successor",
        "_prompt_delivery_loop",
        "_start_terminal_observation_turn",
        "_start_external_result_turn",
    } <= root_chain_callers
