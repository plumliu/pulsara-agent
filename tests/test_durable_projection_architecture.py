from __future__ import annotations

import ast
from pathlib import Path

from pulsara_agent.runtime.blocking_executor import (
    blocking_executor_capacity,
    projection_maintenance_executor,
)


_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = _ROOT / "src" / "pulsara_agent"
_PROJECTION = _SOURCE / "runtime" / "projection_jobs"


def _python_sources(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


def test_removed_projection_hooks_and_workers_are_physically_absent() -> None:
    removed = (
        _SOURCE / "memory" / "hooks" / "run_timeline_persistence.py",
        _SOURCE / "memory" / "hooks" / "runtime_persistence.py",
        _SOURCE / "memory" / "canonical" / "outbox_replay_hook.py",
        _SOURCE / "memory" / "canonical" / "oxigraph_materializer.py",
        _SOURCE / "memory" / "canonical" / "vector_worker.py",
        _SOURCE / "memory" / "canonical" / "reconcile.py",
        _SOURCE / "memory" / "canonical" / "mutation_outbox.py",
    )
    assert not [path for path in removed if path.exists()]
    forbidden_symbols = (
        "RunTimelinePersistenceHook",
        "ExecutionEvidencePersistenceHook",
        "CanonicalMutationOutboxReplayHook",
        "OxigraphCanonicalMutationWorker",
        "CanonicalMutationVectorWorker",
    )
    offenders = {
        str(path.relative_to(_ROOT)): symbol
        for path in _python_sources(_SOURCE)
        for symbol in forbidden_symbols
        if symbol in path.read_text(encoding="utf-8")
    }
    assert offenders == {}


def test_retired_execution_evidence_writer_is_physically_absent() -> None:
    forbidden = (
        "ExecutionEvidenceLedger",
        "record_tool_result_block",
        "record_tool_result_from_event_slice",
        "record_tool_result_from_persisted_event_ref",
        "_record_turn_produced",
    )
    offenders = {
        str(path.relative_to(_ROOT)): token
        for path in _python_sources(_SOURCE)
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    }
    assert offenders == {}


def test_legacy_outbox_is_read_only_migration_authority() -> None:
    allowed = {
        "src/pulsara_agent/storage/migrations/manifest.py",
        "src/pulsara_agent/runtime/projection_jobs/pre_activation.py",
        "src/pulsara_agent/runtime/projection_jobs/migration_transform.py",
        "src/pulsara_agent/runtime/projection_jobs/legacy_mutation_payload.py",
    }
    observed = {
        str(path.relative_to(_ROOT))
        for path in _python_sources(_SOURCE)
        if "memory_write_outbox" in path.read_text(encoding="utf-8")
    }
    assert observed == allowed
    production_text = "\n".join(
        path.read_text(encoding="utf-8") for path in _python_sources(_SOURCE)
    )
    assert "MutationOutboxWriter" not in production_text
    assert "append_payload(" not in production_text


def test_projection_package_uses_process_owned_executor_and_lane() -> None:
    offenders: list[str] = []
    for path in _python_sources(_PROJECTION):
        text = path.read_text(encoding="utf-8")
        if (
            "ThreadPoolExecutor" in text
            or "asyncio.to_thread" in text
            or "PostgresConnectionLane.EVENT_LOG" in text
        ):
            offenders.append(str(path.relative_to(_ROOT)))
    assert offenders == []


def test_projection_executor_is_process_owned_and_bounded() -> None:
    assert projection_maintenance_executor() is projection_maintenance_executor()
    capacity = blocking_executor_capacity()
    assert capacity.projection_maintenance_workers == 9
    assert capacity.critical_ledger_workers == 4


def test_runtime_session_bootstrap_is_the_only_python_session_insert_owner() -> None:
    owners: set[str] = set()
    for path in _python_sources(_SOURCE):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            normalized = " ".join(node.value.lower().split())
            if (
                "insert into sessions" in normalized
                or "insert into public.sessions" in normalized
            ):
                owners.add(str(path.relative_to(_ROOT)))
    assert owners == {"src/pulsara_agent/storage/session_bootstrap.py"}


def test_projection_handlers_are_closed_static_bindings() -> None:
    offenders = {
        str(path.relative_to(_ROOT))
        for path in _python_sources(_PROJECTION)
        if any(
            token in path.read_text(encoding="utf-8")
            for token in ("import_module(", "__import__(")
        )
    }
    assert offenders == set()


def test_publisher_wake_callback_has_no_storage_calls() -> None:
    path = _PROJECTION / "service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    callback = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_published_event"
    )
    call_names = {
        (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else node.func.id
            if isinstance(node.func, ast.Name)
            else ""
        )
        for node in ast.walk(callback)
        if isinstance(node, ast.Call)
    }
    assert call_names <= {"append", "get", "mark_dirty", "set"}


def test_projection_jobs_do_not_scan_full_event_logs_or_emit_agent_events() -> None:
    forbidden = (
        "event_log.iter(",
        "tuple(event_log",
        "list(event_log",
        ".write_event(",
        ".write_events(",
        ".emit(",
    )
    offenders = {
        str(path.relative_to(_ROOT)): token
        for path in _python_sources(_PROJECTION)
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    }
    assert offenders == {}


def test_owned_graph_relations_only_use_the_immutable_relation_port() -> None:
    ledger = _SOURCE / "memory" / "canonical" / "ledger.py"
    ledger_text = ledger.read_text(encoding="utf-8")
    assert "rt.PRODUCED" not in ledger_text
    assert "rt.PROVIDES" not in ledger_text

    allowed = {
        "src/pulsara_agent/entities/runtime/turn.py",
        "src/pulsara_agent/graph/projection_relations.py",
        "src/pulsara_agent/runtime/projection_jobs/projection_handlers.py",
        "src/pulsara_agent/storage/postgres_memory_projection.py",
    }
    observed = {
        str(path.relative_to(_ROOT))
        for path in _python_sources(_SOURCE)
        if "rt.PRODUCED" in path.read_text(encoding="utf-8")
        or "rt.PROVIDES" in path.read_text(encoding="utf-8")
    }
    assert observed == allowed


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_compaction_memory_hard_cut_dependency_direction() -> None:
    runtime_compaction = _SOURCE / "runtime" / "compaction"
    forbidden_prefixes = (
        "pulsara_agent.memory.candidates",
        "pulsara_agent.memory.governance",
        "pulsara_agent.ontology.memory",
    )
    offenders = {
        str(path.relative_to(_ROOT)): module
        for path in _python_sources(runtime_compaction)
        for module in _imported_modules(path)
        if module.startswith(forbidden_prefixes)
    }
    assert offenders == {}
    assert not any(
        module.startswith("pulsara_agent.memory.compaction")
        for module in _imported_modules(_SOURCE / "llm" / "commit.py")
    )


def test_compaction_memory_removed_vocabulary_is_physically_absent() -> None:
    forbidden = (
        "ContextCompactionMemoryCandidatesProposedEvent",
        "<memory_candidates_json>",
        "CompactionCandidateProjectionReceipt",
        "parse_compaction_memory_candidates",
    )
    offenders = {
        str(path.relative_to(_ROOT)): token
        for path in _python_sources(_SOURCE)
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    }
    assert offenders == {}
    assert not (_SOURCE / "runtime" / "compaction" / "candidates.py").exists()


def test_compaction_extension_live_capabilities_are_not_pydantic_fields() -> None:
    contract_paths = (
        _SOURCE / "ports" / "compaction_extensions.py",
        _SOURCE / "memory" / "compaction" / "contracts.py",
    )
    protocol_names: set[str] = set()
    for path in contract_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        protocol_names.update(
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(base, ast.Name) and base.id == "Protocol"
                for base in node.bases
            )
        )
    offenders: list[str] = []
    for path in _python_sources(_SOURCE):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            pydantic_model = any(
                isinstance(base, ast.Name)
                and base.id
                in {"BaseModel", "FrozenFactBase", "FrozenRuntimeStateBase"}
                for base in node.bases
            )
            if not pydantic_model:
                continue
            for statement in node.body:
                if not isinstance(statement, ast.AnnAssign):
                    continue
                annotation_names = {
                    child.id
                    for child in ast.walk(statement.annotation)
                    if isinstance(child, ast.Name)
                }
                if annotation_names & protocol_names:
                    offenders.append(
                        f"{path.relative_to(_ROOT)}:{node.name}"
                    )
    assert offenders == []


def test_compaction_result_authority_contains_only_immutable_plans() -> None:
    extension_text = (
        _SOURCE / "ports" / "compaction_extensions.py"
    ).read_text(encoding="utf-8")
    assert "request_event_candidate: FrozenEventWriteCandidate" in extension_text
    assert "request_event: AgentEvent" not in extension_text

    durable_text = (
        _SOURCE / "projection_jobs" / "compaction_memory.py"
    ).read_text(encoding="utf-8")
    forbidden_fields = (
        "ordered_candidate_outbox_rows",
        "expected_job_lease_fingerprint",
        "candidate_payload: CandidatePayload",
        "candidate_payload: PooledMemoryCandidate",
        "candidate_payload: CandidateProjectionOutboxRow",
    )
    assert not [field for field in forbidden_fields if field in durable_text]


def test_compaction_result_settlement_cannot_mutate_background_budget() -> None:
    path = (
        _SOURCE
        / "runtime"
        / "projection_jobs"
        / "compaction_memory_settlement.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        normalized = " ".join(node.value.lower().split())
        if "background_derived_work_budget_" not in normalized:
            continue
        if any(
            statement in normalized
            for statement in ("insert into", "update ", "delete from")
        ):
            forbidden.append(normalized)
    assert forbidden == []
