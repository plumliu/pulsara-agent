import ast
import asyncio
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock
from typing import get_args

import pytest

from pulsara_agent.primitives.authority_materialization import (
    LedgerWriteAdmissionClass,
)
from pulsara_agent.event.events import AgentEvent, EventBase
from pulsara_agent.llm.raw_provider import (
    RawProviderBlockEnd,
    RawProviderBlockStart,
    RawProviderDataDelta,
    RawProviderFailure,
    RawProviderTextDelta,
    RawProviderThinkingDelta,
    RawProviderToolCallDelta,
)
from pulsara_agent.runtime.authority_materialization.contracts import (
    build_default_transcript_event_domain_registry_binding,
)
from pulsara_agent.runtime.authority_materialization.dispatch_barrier import (
    CheckpointDispatchBarrierCoordinator,
    CheckpointDispatchGateClosed,
)
from pulsara_agent.runtime.context_input.io_service import ContextInputIoService
from pulsara_agent.runtime.event_write_service import (
    PendingRuntimeEventWriteError,
    RuntimeEventWriteService,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "src" / "pulsara_agent" / "runtime"
TOOLS_DIR = REPO_ROOT / "src" / "pulsara_agent" / "tools"
MEMORY_GOVERNANCE_DIR = (
    REPO_ROOT / "src" / "pulsara_agent" / "memory" / "governance"
)


def test_model_stream_delta_events_are_physically_deleted() -> None:
    forbidden = (
        "TextBlockDeltaEvent",
        "ThinkingBlockDeltaEvent",
        "DataBlockDeltaEvent",
        "ToolCallDeltaEvent",
        "EventType.TEXT_BLOCK_DELTA",
        "EventType.THINKING_BLOCK_DELTA",
        "EventType.DATA_BLOCK_DELTA",
        "EventType.TOOL_CALL_DELTA",
    )
    violations: list[str] = []
    source_root = REPO_ROOT / "src" / "pulsara_agent"
    for path in sorted(source_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for symbol in forbidden:
            if symbol in source:
                violations.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{symbol}"
                )
    assert violations == []


def test_provider_adapters_only_emit_adapter_private_raw_items() -> None:
    adapter_root = REPO_ROOT / "src" / "pulsara_agent" / "llm" / "adapters"
    forbidden = (
        "AgentEvent",
        "TextBlockSegmentEvent",
        "ThinkingBlockSegmentEvent",
        "DataBlockSegmentEvent",
        "ToolCallArgumentsSegmentEvent",
    )
    violations: list[str] = []
    for path in sorted(adapter_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for symbol in forbidden:
            if symbol in source:
                violations.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{symbol}"
                )
    assert violations == []

    raw_types = (
        RawProviderBlockStart,
        RawProviderTextDelta,
        RawProviderThinkingDelta,
        RawProviderDataDelta,
        RawProviderToolCallDelta,
        RawProviderBlockEnd,
        RawProviderFailure,
    )
    durable_types = set(get_args(AgentEvent))
    for raw_type in raw_types:
        assert not issubclass(raw_type, EventBase)
        assert raw_type not in durable_types


def test_responses_adapter_never_synthesizes_tool_call_identity() -> None:
    source = (
        REPO_ROOT
        / "src"
        / "pulsara_agent"
        / "llm"
        / "adapters"
        / "openai"
        / "responses.py"
    ).read_text(encoding="utf-8")
    assert "uuid4" not in source
    assert "transport_tool_call_identity_missing" in source


def test_retry_provenance_has_no_legacy_durable_trace_path() -> None:
    adapter_root = (
        REPO_ROOT
        / "src"
        / "pulsara_agent"
        / "llm"
        / "adapters"
        / "openai"
    )
    events_source = (adapter_root / "events.py").read_text(encoding="utf-8")
    assert "provider_data" not in events_source
    assert 'name="llm.retry"' not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(adapter_root.glob("*.py"))
    )
    assert "ProviderRetrySummaryFact" in events_source


def test_model_stream_segments_are_non_transcript_events() -> None:
    registry = build_default_transcript_event_domain_registry_binding().contract
    by_type = {entry.event_type: entry for entry in registry.supported_events}
    for event_type in (
        "TEXT_BLOCK_SEGMENT",
        "THINKING_BLOCK_SEGMENT",
        "DATA_BLOCK_SEGMENT",
        "TOOL_CALL_ARGUMENTS_SEGMENT",
    ):
        assert by_type[event_type].event_domain == "non_transcript"


def test_runtime_wiring_imports_in_clean_interpreter() -> None:
    subprocess.run(
        [sys.executable, "-c", "import pulsara_agent.runtime.wiring"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_long_horizon_and_context_input_import_in_clean_interpreter() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import pulsara_agent.runtime.long_horizon.status; "
                "import pulsara_agent.runtime.context_input.event_slice"
            ),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_runtime_business_code_does_not_directly_append_to_event_log() -> None:
    append_violations: list[str] = []
    extend_violations: list[str] = []
    offline_writer_suffixes = {
        "runtime/session.py",
        "runtime/long_horizon/checkpoint_doctor.py",
    }

    business_paths = (
        sorted(RUNTIME_DIR.rglob("*.py"))
        + sorted(TOOLS_DIR.rglob("*.py"))
        + sorted(MEMORY_GOVERNANCE_DIR.rglob("*.py"))
    )
    for path in business_paths:
        normalized = path.as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"append", "extend"}
            ):
                continue
            receiver = ast.unparse(node.func.value).lower()
            if "log" not in receiver:
                continue
            if node.func.attr == "append" and not any(
                normalized.endswith(suffix) for suffix in offline_writer_suffixes
            ):
                append_violations.append(f"{normalized}:{node.lineno}:{receiver}")
            if node.func.attr == "extend" and not normalized.endswith(
                "runtime/session.py"
            ):
                extend_violations.append(f"{normalized}:{node.lineno}:{receiver}")

    assert append_violations == []
    assert extend_violations == []


def test_production_control_never_calls_raw_model_stream_materializer() -> None:
    violations: list[str] = []
    source_root = REPO_ROOT / "src" / "pulsara_agent"
    diagnostic_path = source_root / "llm" / "diagnostic_materialize.py"
    raw_backend_path = source_root / "llm" / "materialize.py"
    forbidden_names = {
        "materialize_committed_model_call_result",
        "materialize_committed_model_call_result_from_events",
        "_materialize_committed_model_call_result_from_raw_event_log",
        "_materialize_committed_model_call_result_from_raw_events",
    }
    for path in sorted(source_root.rglob("*.py")):
        if path in {diagnostic_path, raw_backend_path}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "pulsara_agent.llm.diagnostic_materialize"
                or any(alias.name in forbidden_names for alias in node.names)
            ):
                violations.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
                )
                continue
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if name in forbidden_names:
                violations.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
                )
    assert violations == []


def test_runtime_business_code_does_not_use_hook_manager_dispatch_as_main_path() -> None:
    violations: list[str] = []

    for path in sorted(RUNTIME_DIR.rglob("*.py")):
        normalized = path.as_posix()
        if normalized.endswith("runtime/hooks.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if "dispatch_observer_event(" in text:
            violations.append(normalized)

    assert violations == []


def test_long_horizon_budget_has_no_fixed_aggregate_second_truth() -> None:
    violations: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "pulsara_agent").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "tool_result_context_chars" in text or "36_000" in text:
            violations.append(path.relative_to(REPO_ROOT).as_posix())

    assert violations == []


def test_long_horizon_semantic_truth_is_not_stored_in_runtime_scratchpad() -> None:
    forbidden_fragments = (
        "context_window_state",
        "window_projection",
        "rollout_account",
        "rollout_state",
        "status_hint",
        "recurrence_state",
        "compaction_plan",
    )
    violations: list[str] = []

    for path in sorted(RUNTIME_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            key = _literal_scratchpad_key(node)
            if key is not None and any(
                fragment in key for fragment in forbidden_fragments
            ):
                violations.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{getattr(node, 'lineno', 0)}:{key}"
                )

    assert violations == []


def test_long_horizon_production_hot_paths_do_not_full_scan_event_logs() -> None:
    targets = (
        ("src/pulsara_agent/host/session.py", "HostSession", "_prepare_prior_messages_for_turn"),
        ("src/pulsara_agent/runtime/subagent/runtime.py", "SubagentRuntime", "__init__"),
        ("src/pulsara_agent/runtime/long_horizon/accounting.py", None, "resolve_run_rollout_binding"),
        ("src/pulsara_agent/llm/execution.py", "ModelStreamExecutionHandle", "subscribe"),
        ("src/pulsara_agent/llm/control.py", "RunModelCallControlOwner", "_validate_durable_result_attribution"),
        ("src/pulsara_agent/runtime/long_horizon/window_compaction_service.py", "ContextWindowCompactionService", "compact"),
        ("src/pulsara_agent/runtime/long_horizon/window_compaction_service.py", "ContextWindowCompactionService", "recover_interrupted"),
        ("src/pulsara_agent/runtime/long_horizon/window_compaction_service.py", None, "_validate_source_refs"),
        ("src/pulsara_agent/runtime/context_input/live.py", None, "_read_live_primary_event_slice"),
        ("src/pulsara_agent/runtime/session.py", "RuntimeSession", "_validate_run_lifecycle_batch"),
    )
    violations: list[str] = []
    for relative, class_name, function_name in targets:
        path = REPO_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        node = _find_callable(tree, class_name=class_name, name=function_name)
        if any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == "iter"
            for item in ast.walk(node)
        ):
            violations.append(f"{relative}:{class_name or '<module>'}.{function_name}")
    assert violations == []

    for relative in (
        "src/pulsara_agent/runtime/context_input/live.py",
        "src/pulsara_agent/runtime/compaction/service.py",
    ):
        tree = ast.parse(
            (REPO_ROOT / relative).read_text(encoding="utf-8"), filename=relative
        )
        module_iter_calls = [
            item
            for item in ast.walk(tree)
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == "iter"
        ]
        assert module_iter_calls == [], relative
        for item in ast.walk(tree):
            if not (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr == "read_raw_range_snapshot"
            ):
                continue
            keyword_names = {keyword.arg for keyword in item.keywords}
            assert {"max_events", "max_payload_bytes"}.issubset(keyword_names), (
                relative,
                getattr(item, "lineno", 0),
            )

    host_path = REPO_ROOT / "src/pulsara_agent/host/session.py"
    host_tree = ast.parse(host_path.read_text(encoding="utf-8"), filename=str(host_path))
    pre_run = _find_callable(
        host_tree,
        class_name="HostSession",
        name="_prepare_prior_messages_for_turn",
    )
    forbidden_legacy_helpers = {
        "_prior_messages",
        "read_event_snapshot_through_current",
        "rebuild_prior_messages",
        "rebuild_prior_messages_before_sequence",
    }
    called_names = {
        item.func.id
        for item in ast.walk(pre_run)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
    } | {
        item.func.attr
        for item in ast.walk(pre_run)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
    }
    assert called_names.isdisjoint(forbidden_legacy_helpers)


def test_agent_host_and_subagent_modules_default_deny_full_event_log_scans() -> None:
    allowed_repair_or_offline_owners = {
        "src/pulsara_agent/runtime/agent.py": frozenset(),
        "src/pulsara_agent/host/session.py": frozenset({"replay_events"}),
        "src/pulsara_agent/runtime/subagent/runtime.py": frozenset(
            {"repair_dangling_children"}
        ),
    }
    violations: list[str] = []
    for relative, allowed_owners in allowed_repair_or_offline_owners.items():
        path = REPO_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = tuple(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "iter"
            ):
                continue
            owners = tuple(
                function
                for function in functions
                if function.lineno <= node.lineno <= function.end_lineno
            )
            owner = min(
                owners,
                key=lambda function: function.end_lineno - function.lineno,
                default=None,
            )
            owner_name = owner.name if owner is not None else "<module>"
            if owner_name not in allowed_owners:
                violations.append(f"{relative}:{node.lineno}:{owner_name}")
    assert violations == []


def test_full_raw_transcript_fold_only_exists_in_privileged_doctor() -> None:
    authority_dir = RUNTIME_DIR / "authority_materialization"
    violations: list[str] = []
    allowed = authority_dir / "doctor.py"
    for path in sorted(authority_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "read_raw_range_snapshot"
            ):
                continue
            minimum = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "minimum_sequence"
                ),
                None,
            )
            if (
                isinstance(minimum, ast.Constant)
                and minimum.value == 1
                and path != allowed
            ):
                violations.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
                )
    assert violations == []
    doctor_text = allowed.read_text(encoding="utf-8")
    assert "maintenance_authority.acquire_exclusive" in doctor_text
    assert "max_events=max_events" in doctor_text
    assert "max_payload_bytes=max_payload_bytes" in doctor_text

    live_text = (
        RUNTIME_DIR / "context_input/live.py"
    ).read_text(encoding="utf-8")
    live_reader = live_text.split(
        "async def _read_live_primary_event_slice(", 1
    )[1].split("\ndef _contiguous_exact_slices(", 1)[0]
    assert "projection_delta_minimum_sequence()" in live_reader
    assert "minimum_sequence = 1" not in live_reader
    assert "if compacted_window else ()" not in live_reader


def test_authority_hard_cut_has_no_legacy_account_migration_path() -> None:
    authority_dir = RUNTIME_DIR / "authority_materialization"
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(authority_dir.glob("*.py"))
    )
    assert "requires migration" not in text
    assert "legacy account" not in text
    assert "backfill_materialization" not in text


def test_model_call_and_transcript_reads_keep_physical_bounds_and_indexes() -> None:
    schema = (REPO_ROOT / "src/pulsara_agent/storage/postgres_schema.py").read_text(
        encoding="utf-8"
    )
    materialize = (REPO_ROOT / "src/pulsara_agent/llm/materialize.py").read_text(
        encoding="utf-8"
    )
    transcript = (REPO_ROOT / "src/pulsara_agent/runtime/transcript.py").read_text(
        encoding="utf-8"
    )
    assert "idx_agent_events_session_model_call_sequence" in schema
    assert "idx_agent_events_session_type_sequence" in schema
    assert "MAX_MODEL_CALL_MATERIALIZATION_EVENTS" in materialize
    assert "MAX_MODEL_CALL_MATERIALIZATION_PAYLOAD_BYTES" in materialize
    assert "_MAX_TRANSCRIPT_CONTROL_EVENTS" in transcript
    assert "read_raw_replies_snapshot(" in transcript
    assert "read_raw_reply_events(" not in transcript
    commit = (REPO_ROOT / "src/pulsara_agent/llm/commit.py").read_text(
        encoding="utf-8"
    )
    assert "event_log.iter(" not in commit
    assert "event_log.get_by_id(" not in commit
    assert "read_raw_events_by_id(" not in commit
    assert "live_cursor" in commit
    graph_contract = (
        REPO_ROOT / "src/pulsara_agent/primitives/long_horizon.py"
    ).read_text(encoding="utf-8")
    assert "MAX_MODEL_CALL_MATERIALIZATION_EVENTS" not in graph_contract
    assert "MAX_MODEL_CALL_MATERIALIZATION_PAYLOAD_BYTES" not in graph_contract
    assert "MAX_TRANSPORT_SOURCE_ITEMS_PER_MODEL_CALL" not in graph_contract
    assert "MAX_SANITIZED_SOURCE_PAYLOAD_BYTES_PER_MODEL_CALL" not in graph_contract
    live = (REPO_ROOT / "src/pulsara_agent/runtime/context_input/live.py").read_text(
        encoding="utf-8"
    )
    compaction = (
        REPO_ROOT / "src/pulsara_agent/runtime/compaction/service.py"
    ).read_text(encoding="utf-8")
    assert "max_events=_MAX_LIVE_AUTHORITY_EVENTS" in live
    assert "max_payload_bytes=_MAX_LIVE_AUTHORITY_PAYLOAD_BYTES" in live
    assert "max_events=_MAX_COMPACTION_SOURCE_EVENTS" in compaction
    assert "max_payload_bytes=_MAX_COMPACTION_SOURCE_BYTES" in compaction


def test_runtime_async_event_writes_use_one_session_fifo_writer() -> None:
    session_path = REPO_ROOT / "src/pulsara_agent/runtime/session.py"
    tree = ast.parse(session_path.read_text(encoding="utf-8"), filename=str(session_path))
    write_events = _find_callable(
        tree,
        class_name="RuntimeSession",
        name="write_events",
    )
    calls = {
        item.func.attr
        for item in ast.walk(write_events)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
    }
    assert "execute" in calls
    assert "execute_blocking" not in calls

    service = (
        REPO_ROOT / "src/pulsara_agent/runtime/event_write_service.py"
    ).read_text(encoding="utf-8")
    assert "critical_ledger_executor()" in service
    assert "ThreadPoolExecutor(" not in service


def test_compacted_authority_and_rollup_cache_do_not_rebind_full_transcript() -> None:
    live = (
        REPO_ROOT / "src/pulsara_agent/runtime/context_input/live.py"
    ).read_text(encoding="utf-8")
    rollup = (
        REPO_ROOT / "src/pulsara_agent/runtime/long_horizon/rollup.py"
    ).read_text(encoding="utf-8")
    assert "source_through + 1" in live
    assert "ContextEventAuthorityView" in live
    cache_key = ast.parse(rollup, filename="rollup.py")
    key_builder = _find_callable(
        cache_key,
        class_name=None,
        name="prepared_observation_rollup_cache_key",
    )
    parameter_names = {argument.arg for argument in key_builder.args.kwonlyargs}
    assert "placement_basis_fingerprint" in parameter_names
    assert "transcript_fingerprint" not in parameter_names


def test_runtime_blocking_services_reserve_a_critical_ledger_lane() -> None:
    for relative in (
        "src/pulsara_agent/runtime/context_input/io_service.py",
        "src/pulsara_agent/runtime/context_input/manifest.py",
    ):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "auxiliary_io_executor()" in text, relative
        assert "critical_ledger_executor()" not in text, relative
        assert "ThreadPoolExecutor(" not in text, relative
    writer = (
        REPO_ROOT / "src/pulsara_agent/runtime/event_write_service.py"
    ).read_text(encoding="utf-8")
    assert "critical_ledger_executor()" in writer
    assert "auxiliary_io_executor()" not in writer
    assert "ThreadPoolExecutor(" not in writer


def test_event_log_readers_use_lane_aware_pool_and_no_direct_connect() -> None:
    postgres = (
        REPO_ROOT / "src/pulsara_agent/event_log/postgres.py"
    ).read_text(encoding="utf-8")
    pool = (
        REPO_ROOT / "src/pulsara_agent/event_log/postgres_pool.py"
    ).read_text(encoding="utf-8")
    assert "psycopg.connect(" not in postgres
    assert "PostgresConnectionLane.BOUNDED_READ" in postgres
    assert "_CRITICAL_WRITE_RESERVE" in pool
    assert "BoundedSemaphore" in pool


def test_live_authority_hot_path_uses_one_bundle_read() -> None:
    path = REPO_ROOT / "src/pulsara_agent/runtime/context_input/live.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    target = _find_callable(tree, class_name=None, name="_read_live_primary_event_slice")
    calls = {
        item.func.attr
        for item in ast.walk(target)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
    }
    assert "read_context_authority_bundle" in calls
    assert "next_sequence" not in calls
    assert "read_raw_range_snapshot" not in calls
    assert "read_raw_events_by_types" not in calls


def test_auxiliary_io_saturation_does_not_starve_critical_ledger_lane() -> None:
    async def exercise() -> None:
        auxiliary = ContextInputIoService(max_pending=12)
        writer = RuntimeEventWriteService(operation_timeout_seconds=1.0)
        release = Event()
        all_started = Event()
        counter_lock = Lock()
        started = 0

        def block_auxiliary() -> str:
            nonlocal started
            with counter_lock:
                started += 1
                if started == 12:
                    all_started.set()
            release.wait()
            return "released"

        tasks = tuple(
            asyncio.create_task(
                auxiliary.execute(
                    operation_name=f"saturate-auxiliary-{index}",
                    operation=block_auxiliary,
                    deadline_monotonic=asyncio.get_running_loop().time() + 2.0,
                )
            )
            for index in range(12)
        )
        assert await asyncio.to_thread(all_started.wait, 1.0)
        assert await writer.execute(lambda: "committed") == "committed"
        release.set()
        assert await asyncio.gather(*tasks) == ["released"] * 12
        await auxiliary.drain_pending(
            deadline_monotonic=asyncio.get_running_loop().time() + 1.0
        )
        auxiliary.close_if_idle()
        writer.close_if_idle()

    asyncio.run(exercise())


def test_event_writer_queue_deadline_expires_before_physical_start() -> None:
    async def exercise() -> None:
        writer = RuntimeEventWriteService(operation_timeout_seconds=1.0)
        entered = Event()
        release = Event()

        def first() -> str:
            entered.set()
            release.wait()
            return "first"

        first_task = asyncio.create_task(writer.execute(first))
        assert await asyncio.to_thread(entered.wait, 1.0)
        second = asyncio.create_task(
            writer.execute(
                lambda: "second",
                deadline_monotonic=asyncio.get_running_loop().time() + 0.02,
            )
        )
        with pytest.raises(PendingRuntimeEventWriteError, match="while queued"):
            await asyncio.wait_for(second, timeout=0.2)
        assert not release.is_set()
        release.set()
        assert await first_task == "first"
        writer.close_if_idle()

    asyncio.run(exercise())


def test_checkpoint_drain_closes_new_writers_and_waits_admitted_fifo() -> None:
    async def exercise() -> None:
        gate = CheckpointDispatchBarrierCoordinator()
        writer = RuntimeEventWriteService(operation_timeout_seconds=1.0)
        writer.bind_admission_coordinator(gate)
        entered = Event()
        release = Event()

        def admitted_writer() -> str:
            entered.set()
            assert release.wait(timeout=1)
            return "admitted"

        admitted = asyncio.create_task(writer.execute(admitted_writer))
        assert await asyncio.to_thread(entered.wait, 1)
        drain = gate.begin_checkpoint_drain(
            checkpoint_id="checkpoint:dispatch-gate",
            checkpoint_candidate_fingerprint="sha256:" + "a" * 64,
        )
        drain_waiter = asyncio.create_task(
            asyncio.to_thread(
                gate.wait_until_drained,
                drain,
                deadline_monotonic=asyncio.get_running_loop().time() + 1,
            )
        )
        await asyncio.sleep(0)
        assert not drain_waiter.done()
        with pytest.raises(CheckpointDispatchGateClosed, match="new producer"):
            await writer.execute(lambda: "must-not-start")

        release.set()
        assert await admitted == "admitted"
        await drain_waiter
        assert gate.active_producer_count == 0
        assert (
            await writer.execute(
                lambda: "checkpoint-control",
                admission_class=(
                    LedgerWriteAdmissionClass.CHECKPOINT_BARRIER_CONTROL
                ),
                checkpoint_id=drain.checkpoint_id,
            )
            == "checkpoint-control"
        )
        gate.abort_before_install(drain)
        assert await writer.execute(lambda: "producer-reopened") == "producer-reopened"
        writer.close_if_idle()

    asyncio.run(exercise())


def test_checkpoint_barrier_drains_promoted_operation_owners_before_freeze() -> None:
    async def exercise() -> None:
        gate = CheckpointDispatchBarrierCoordinator()
        writer = RuntimeEventWriteService(operation_timeout_seconds=1.0)
        writer.bind_admission_coordinator(gate)

        def dispatch() -> tuple[object, ...]:
            return writer.promote_current_producer_admission(
                operation_owner_ids=("model:call-1", "tool:call-2"),
            )

        model_token, tool_token = await writer.execute(dispatch)
        assert gate.active_producer_count == 2
        drain = gate.begin_checkpoint_drain(
            checkpoint_id="checkpoint:operation-drain",
            checkpoint_candidate_fingerprint="sha256:" + "b" * 64,
        )
        waiter = asyncio.create_task(
            asyncio.to_thread(
                gate.wait_until_drained,
                drain,
                deadline_monotonic=asyncio.get_running_loop().time() + 1,
            )
        )
        await asyncio.sleep(0)
        assert not waiter.done()
        gate.release_write_admission(model_token)
        await asyncio.sleep(0)
        assert not waiter.done()
        gate.release_write_admission(tool_token)
        await waiter
        gate.abort_before_install(drain)
        writer.close_if_idle()

    asyncio.run(exercise())


def test_checkpoint_drain_allows_only_exact_operation_continuation() -> None:
    async def exercise() -> None:
        gate = CheckpointDispatchBarrierCoordinator()
        writer = RuntimeEventWriteService(operation_timeout_seconds=1.0)
        writer.bind_admission_coordinator(gate)

        def dispatch():
            return writer.promote_current_producer_admission(
                operation_owner_ids=("model:call-1",)
            )[0]

        operation_token = await writer.execute(dispatch)
        drain = gate.begin_checkpoint_drain(
            checkpoint_id="checkpoint:continuation",
            checkpoint_candidate_fingerprint="sha256:" + "c" * 64,
        )
        with pytest.raises(CheckpointDispatchGateClosed, match="exact admitted owner"):
            await writer.execute(
                lambda: None,
                admission_class=LedgerWriteAdmissionClass.OPERATION_CONTINUATION,
                operation_owner_id="model:wrong-call",
            )

        def settle() -> str:
            gate.release_write_admission(operation_token)
            return "settled"

        assert (
            await writer.execute(
                settle,
                admission_class=LedgerWriteAdmissionClass.OPERATION_CONTINUATION,
                operation_owner_id="model:call-1",
            )
            == "settled"
        )
        gate.wait_until_drained(
            drain,
            deadline_monotonic=asyncio.get_running_loop().time() + 1,
        )
        gate.abort_before_install(drain)
        writer.close_if_idle()

    asyncio.run(exercise())


def test_production_code_does_not_call_owner_only_sync_event_confirmation() -> None:
    violations: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "pulsara_agent").rglob("*.py")):
        if path.as_posix().endswith("runtime/session.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "confirm_event_batch"
            ):
                continue
            violations.append(
                f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
            )
    assert violations == []


def test_runtime_event_writer_is_fifo_without_blocking_event_loop() -> None:
    async def exercise() -> None:
        service = RuntimeEventWriteService()
        started = Event()
        release = Event()
        order: list[str] = []

        def first() -> str:
            order.append("first-start")
            started.set()
            assert release.wait(timeout=2)
            order.append("first-end")
            return "first"

        def second() -> str:
            order.append("second")
            return "second"

        first_task = asyncio.create_task(service.execute(first))
        while not started.is_set():
            await asyncio.sleep(0)
        second_task = asyncio.create_task(service.execute(second))
        await asyncio.sleep(0.01)
        assert order == ["first-start"]
        assert not second_task.done()
        release.set()
        assert await asyncio.gather(first_task, second_task) == ["first", "second"]
        assert order == ["first-start", "first-end", "second"]
        service.close_if_idle()

    asyncio.run(exercise())


def _find_callable(
    tree: ast.Module,
    *,
    class_name: str | None,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    scope: list[ast.stmt] = tree.body
    if class_name is not None:
        owner = next(
            item
            for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == class_name
        )
        scope = owner.body
    return next(
        item
        for item in scope
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )


def _literal_scratchpad_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Subscript) and _is_scratchpad(node.value):
        return _string_constant(node.slice)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and _is_scratchpad(node.func.value)
        and node.func.attr in {"get", "pop", "setdefault"}
        and node.args
    ):
        return _string_constant(node.args[0])
    return None


def _is_scratchpad(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "scratchpad"


def _string_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
