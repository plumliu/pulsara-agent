"""Run the Round 5B real-provider compaction activation dogfood.

The probe owns an ephemeral clean-v0 database and trusted temporary
workspaces.  It records only closed states, counts, fingerprints and sentinel
booleans; credentials, prompts, summaries, ToolResults, Skill bodies, MCP
schemas and opaque references are never printed or persisted in the report.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import yaml

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionTrigger,
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.model_input.continuity import (
    ProviderInputContinuityScope,
    decode_runtime_observation,
)
from pulsara_agent.model_input.contracts import (
    ContextSourceKind,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.settings import PulsaraSettings, StorageConfig, load_env_file
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput


_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "round9_mcp_server.py"
_SKILL_NAME = "round5b-retained-check"
_SENTINEL = "ROUND5B_SKILL_SUCCESS"
_MCP_SENTINEL = "round5b-sentinel"


def _dsn_with_database(dsn: str, database_name: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = database_name
    return make_conninfo(**values)


def _require_loopback_pulsara(dsn: str, *, label: str) -> None:
    values = conninfo_to_dict(dsn)
    host = str(values.get("host", "")).strip().lower()
    database = str(values.get("dbname", "")).strip()
    if host not in {"localhost", "127.0.0.1", "::1"} or database != "pulsara":
        raise RuntimeError(f"{label} must target exact loopback database pulsara")


def _drop_database(admin_root_dsn: str, database_name: str) -> None:
    with psycopg.connect(admin_root_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database_name)
            )
        )


def _create_database(settings: PulsaraSettings) -> tuple[str, str, str]:
    admin_root_dsn = os.getenv("PULSARA_POSTGRES_ADMIN_DSN", "").strip()
    if not admin_root_dsn:
        raise RuntimeError("PULSARA_POSTGRES_ADMIN_DSN is required")
    _require_loopback_pulsara(admin_root_dsn, label="admin DSN")
    _require_loopback_pulsara(settings.storage.postgres_dsn, label="runtime DSN")
    database_name = f"pulsara_round5b_{os.getpid()}_{uuid4().hex[:10]}"
    admin_dsn = _dsn_with_database(admin_root_dsn, database_name)
    runtime_dsn = _dsn_with_database(settings.storage.postgres_dsn, database_name)
    with psycopg.connect(admin_root_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    try:
        runner = PostgresMigrationRunner(
            admin_dsn=admin_dsn,
            runtime_dsn=runtime_dsn,
        )
        first = runner.migrate(deadline_monotonic=monotonic() + 240)
        second = runner.migrate(deadline_monotonic=monotonic() + 240)
        if first.applied_versions != (0,) or second.applied_versions != ():
            raise RuntimeError("ephemeral clean-v0 migration was not repeatable")
    except BaseException:
        _drop_database(admin_root_dsn, database_name)
        raise
    return admin_root_dsn, database_name, runtime_dsn


def _runtime_settings(env_file: str, runtime_dsn: str) -> PulsaraSettings:
    load_env_file(env_file, override=False)
    os.environ["PULSARA_MEMORY_AUTO_DENSE"] = "false"
    os.environ["PULSARA_MEMORY_EXPLICIT_RERANK"] = "false"
    os.environ["PULSARA_MEMORY_CHEAP_HINT_REFLECTION"] = "false"
    settings = PulsaraSettings.from_env()
    return replace(
        settings,
        storage=StorageConfig(postgres_dsn=runtime_dsn),
    )


def _write_mcp_config(
    workspace: Path,
    servers: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    values: dict[str, object] = {"servers": {}}
    entries = values["servers"]
    assert isinstance(entries, dict)
    for server_id, names in servers:
        entries[server_id] = {
            "enabled": True,
            "required": True,
            "scope_policy": "ROOT_ONLY",
            "supports_parallel_tool_calls": False,
            "catalog_refresh_interval_ms": "DISABLED",
            "exposure_policy": {
                "include_tool_names": list(names),
                "invalid_tool_policy": "FAIL_SERVER",
            },
            "transport": {
                "type": "stdio",
                "command": os.sys.executable,
                "args": [os.fspath(_FIXTURE)],
            },
        }
    target = workspace / ".pulsara" / "mcp.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(values, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )


def _write_skill(workspace: Path, *, changed: bool = False) -> Path:
    target = workspace / ".pulsara" / "skills" / _SKILL_NAME / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    instruction = (
        "After this file body is available, call search_files on this SKILL.md "
        "once for the exact phrase 'Retention checkpoint sentinel'. Wait for its "
        "result. Then call the "
        "native MCP tool mcp__late__direct_echo exactly once with the text value "
        f"{_MCP_SENTINEL!r}. After its result, answer with {_SENTINEL}."
        if not changed
        else "This manifest was intentionally changed after the completed run."
    )
    target.write_text(
        "---\n"
        f"name: {_SKILL_NAME}\n"
        "description: A bounded workflow used only for the Round 5B retained "
        "Skill integration check.\n"
        "---\n\n"
        "# Retained context integration check\n\n"
        "Retention checkpoint sentinel.\n\n"
        f"{instruction}\n",
        encoding="utf-8",
    )
    return target


def _isolated_mcp_configs(workspace: Path):
    from pulsara_agent.mcp_config import load_mcp_server_configs

    return load_mcp_server_configs(
        workspace_root=workspace,
        user_config_path=workspace / ".missing-user-mcp.yaml",
        trust_workspace_config=True,
    )


def _scope(session) -> ProviderInputContinuityScope:
    return ProviderInputContinuityScope(
        session_id=session.session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )


def _current_epoch(session):
    view = session._input_continuity.current_view(_scope(session))  # noqa: SLF001
    if view is None:
        raise RuntimeError("real-provider dogfood has no installed epoch")
    return view


def _route_counts(view) -> dict[str, int]:
    result = {"DIRECT": 0, "NEW_MCP_META_ONLY": 0, "UNAVAILABLE": 0}
    for route in view.mcp_route_projection.routes:
        result[route.route.value] += 1
    return result


def _route_details(view) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (
            item.target.server_id,
            item.target.remote_tool_name,
            item.route.value,
            item.public_reason_code.value,
        )
        for item in view.mcp_route_projection.routes
    )


def _runtime_source_presence(view, kind: ContextSourceKind) -> str | None:
    matches = [item for item in view.source_heads if item.source_kind is kind]
    if len(matches) != 1:
        return None
    return matches[0].presence.value


def _runtime_source_body_contains(view, kind: ContextSourceKind, text: str) -> bool:
    for message in view.messages:
        try:
            observation = decode_runtime_observation(message)
        except ValueError:
            continue
        if observation.source_kind is kind and text in observation.body:
            return True
    return False


def _seed_completed_history(session, *, segments: int = 5) -> None:
    guard = session._lease.guard  # noqa: SLF001
    repository = session.repository
    for index in range(segments):
        suffix = uuid4().hex
        turn_id = f"turn:round5b-seed:{suffix}"
        repository.start_root_turn(
            guard,
            command_id=f"command:round5b-seed:{suffix}",
            turn_id=turn_id,
            entry_id=f"entry:round5b-seed-user:{suffix}",
            context_binding_revision_id=f"revision:round5b-seed:{suffix}",
            permission_snapshot_id=f"permission:round5b-seed:{suffix}",
            requested_permission_mode=DEFAULT_PERMISSION_MODE,
            content=InlineContent.from_bytes(
                (f"historical-segment-{index}:" + " context" * 6_000).encode()
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id="round5b-dogfood",
            deadline_monotonic=monotonic() + 60,
        )
        cut = repository.prepare_provider_input_cut(
            guard,
            turn_id=turn_id,
            deadline_monotonic=monotonic() + 60,
        )
        assistant_id = f"entry:round5b-seed-assistant:{suffix}"
        repository.commit_assistant_message(
            guard,
            cut=cut,
            entry_id=assistant_id,
            parent_content=InlineContent.from_bytes(b"seed complete"),
            blocks=(
                AssistantTextBlock(
                    block_id=f"block:round5b-seed:{suffix}",
                    text=InlineContent.from_bytes(b"seed complete"),
                ),
            ),
            provider_wire_api="openai_chat_completions",
            complete_turn=True,
            occurred_at=datetime.now(timezone.utc),
            actor_id="round5b-dogfood",
            deadline_monotonic=monotonic() + 60,
        )


class _RecordingTransport:
    def __init__(self, delegate, on_open) -> None:
        self._delegate = delegate
        self._on_open = on_open
        self.binding_id = delegate.binding_id
        self.contract_version = delegate.contract_version

    def open_stream(self, *, call, context):
        self._on_open(context)
        return self._delegate.open_stream(call=call, context=context)


def _install_summary_recorder(session, records: list[dict[str, object]]) -> None:
    model = session._model  # noqa: SLF001
    original = model.resolve_compaction_summary_call

    def resolve(**kwargs):
        call = original(**kwargs)

        def opened(context) -> None:
            old = _current_epoch(session)
            materialization = context.provider_wire_input_plan.materialization
            old_wire = old.wire_input_plan.materialization
            overlap = min(
                len(materialization.ordered_input_items) - 1,
                len(old_wire.ordered_input_items),
            )
            records.append(
                {
                    "old_epoch_nonce": old.epoch_nonce,
                    "old_epoch_revision": old.epoch_revision,
                    "old_semantic_prefix": old.semantic_prefix_fingerprint,
                    "old_route_counts": _route_counts(old),
                    "tool_choice_none": context.tool_choice_none,
                    "tools_exact": materialization.tool_items == old_wire.tool_items,
                    "wire_prefix_exact": (
                        materialization.ordered_input_items[:overlap]
                        == old_wire.ordered_input_items[:overlap]
                    ),
                    "input_tokens": context.compiler_estimated_input_tokens,
                }
            )

        transport = _RecordingTransport(call.target.transport, opened)
        return replace(call, target=replace(call.target, transport=transport))

    model.resolve_compaction_summary_call = resolve


def _install_read_activation(session, observed_tools: list[str]) -> None:
    tools = session._tools  # noqa: SLF001
    original = tools.invoke

    async def invoke(**kwargs):
        result = await original(**kwargs)
        name = str(kwargs["tool_name"])
        observed_tools.append(name)
        if name == "search_files" and observed_tools.count("search_files") == 1:
            session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
                automatic_enabled=True,
                auto_trigger_ratio=0.30,
                post_compaction_target_ratio=0.20,
                minimum_reclaim_tokens=1,
                maximum_retained_tool_groups=1,
            )
        return result

    tools.invoke = invoke


def _install_compaction_trigger_recorder(
    session,
    records: list[dict[str, object]],
    observed_tools: list[str],
) -> None:
    """Record the exact safe-point trigger without observing model content."""

    runner = session._runner  # noqa: SLF001
    original = runner._execute_active_compaction  # noqa: SLF001

    async def execute(**kwargs):
        before = _snapshot_fingerprints(session)
        observed_before = tuple(observed_tools)
        outcome = await original(**kwargs)
        after = _snapshot_fingerprints(session)
        records.append(
            {
                "trigger": kwargs["trigger"].value,
                "turn_id": kwargs["turn_id"],
                "model_call_index": kwargs["model_call_index"],
                "tool_calls_before_compaction": len(observed_before),
                "tool_names_before_compaction": observed_before,
                "snapshot_count_before": len(before),
                "snapshot_count_after": len(after),
                "disposition": outcome.disposition.value,
                "snapshot_id_present": outcome.snapshot_id is not None,
                "revision_ordinal": outcome.revision_ordinal,
            }
        )
        return outcome

    runner._execute_active_compaction = execute  # noqa: SLF001


def _snapshot_fingerprints(session) -> tuple[str, ...]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT content_digest FROM pulsara_v3.context_snapshots "
            "WHERE session_id = %s ORDER BY created_at, id",
            (session.session_id,),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _tool_trajectory(session) -> tuple[str, ...]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT b.tool_name FROM pulsara_v3.assistant_message_blocks b "
            "JOIN pulsara_v3.transcript_entries e "
            "ON e.session_id=b.session_id AND e.id=b.assistant_entry_id "
            "WHERE b.session_id=%s AND b.block_kind='TOOL_CALL' "
            "ORDER BY e.entry_sequence,b.block_ordinal",
            (session.session_id,),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _tool_trajectory_for_turn(session, turn_id: str) -> tuple[str, ...]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT b.tool_name FROM pulsara_v3.assistant_message_blocks b "
            "JOIN pulsara_v3.transcript_entries e "
            "ON e.session_id=b.session_id AND e.id=b.assistant_entry_id "
            "WHERE b.session_id=%s AND e.turn_id=%s "
            "AND b.block_kind='TOOL_CALL' "
            "ORDER BY e.entry_sequence,b.block_ordinal",
            (session.session_id, turn_id),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


async def _run_retained_and_repeated(
    *, settings: PulsaraSettings, workspace: Path
) -> dict[str, object]:
    import pulsara_agent.conversation_kernel.host as host_module
    from pulsara_agent.capability.local_skills import LocalSkillProvider
    from pulsara_agent.capability.resolver import LocalSkillCapabilityProvider

    _write_mcp_config(workspace, (("direct", ("direct_echo",)),))
    _write_skill(workspace)
    original_loader = host_module.load_mcp_server_configs
    host_module.load_mcp_server_configs = lambda **_: _isolated_mcp_configs(workspace)
    core = KernelHostCore.production(settings=settings)
    summary_records: list[dict[str, object]] = []
    compaction_records: list[dict[str, object]] = []
    observed_tools: list[str] = []
    try:
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=True,
            ),
            system_prompt=(
                "You are performing a bounded local product integration task. "
                "Use catalog and tool observations accurately, and keep final "
                "answers brief."
            ),
        )
        session._capabilities._provider = LocalSkillCapabilityProvider(  # noqa: SLF001
            provider=LocalSkillProvider(include_user_skills=False)
        )
        session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
        ready = await session._mcp_supervisor.wait_for_server_settlement(  # noqa: SLF001
            "direct", timeout_seconds=20
        )
        if ready.value != "READY":
            raise RuntimeError("initial MCP fixture did not become READY")
        await session.run_turn(
            "Acknowledge this integration preflight briefly without using a tool.",
            command_id="command:round5b:preflight",
        )
        initial_epoch = _current_epoch(session)

        _write_mcp_config(
            workspace,
            (("direct", ("direct_echo",)), ("late", ("direct_echo",))),
        )
        await session.reload_mcp_configs(_isolated_mcp_configs(workspace))
        ready = await session._mcp_supervisor.wait_for_server_settlement(  # noqa: SLF001
            "late", timeout_seconds=20
        )
        if ready.value != "READY":
            raise RuntimeError("late MCP fixture did not become READY")
        inspected = await session.run_turn(
            "Inspect the newly listed late-server direct_echo tool so its exact "
            "argument shape is available for a later task. Do not execute it yet.",
            command_id="command:round5b:inspect",
        )
        del inspected
        before_compaction = _current_epoch(session)
        old_ref_tokens = tuple(session._tools._mcp_meta_refs._refs)  # noqa: SLF001
        if not old_ref_tokens:
            raise RuntimeError("provider did not install an exact MCP inspect ref")

        _seed_completed_history(session)
        _install_summary_recorder(session, summary_records)
        _install_read_activation(session, observed_tools)
        _install_compaction_trigger_recorder(
            session,
            compaction_records,
            observed_tools,
        )
        skill_result = await session.run_turn(
            f"Use the cataloged {_SKILL_NAME} workflow for this bounded check. "
            "If its exact body is not already present in current model input, "
            f"call read_file on .pulsara/skills/{_SKILL_NAME}/SKILL.md with "
            "offset 1 and limit 2000 exactly once. If the body is already "
            "present, do not read it again. Then follow that workflow.",
            command_id="command:round5b:skill",
        )
        first_successor = _current_epoch(session)
        if not summary_records:
            raise RuntimeError(
                "skill run did not trigger compaction: installed_estimate="
                f"{first_successor.final_estimate.total_input_tokens}; tools="
                f"{','.join(observed_tools)}"
            )
        first_snapshots = _snapshot_fingerprints(session)
        try:
            session._tools._mcp_meta_refs.resolve_callable(  # noqa: SLF001
                old_ref_tokens[0],
                conversation_scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                continuity_epoch_nonce=first_successor.epoch_nonce,
            )
        except LookupError:
            old_ref_stale = True
        else:
            old_ref_stale = False

        # A later true ROOT run establishes a second active compaction and a
        # second durable snapshot.  The current correction remains in the
        # protected tail and must override any prior summary.
        _write_skill(workspace, changed=True)
        _seed_completed_history(session, segments=15)
        session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
            automatic_enabled=True,
            minimum_reclaim_tokens=1,
        )
        corrected = await session.run_turn(
            "Correction: the current answer sentinel is BLUE. Reply with BLUE "
            "and do not use any tool.",
            command_id="command:round5b:correction",
        )
        second_successor = _current_epoch(session)
        all_snapshots = _snapshot_fingerprints(session)
        trajectory = _tool_trajectory(session)
        skill_turn_trajectory = _tool_trajectory_for_turn(
            session,
            skill_result.turn_id,
        )
        mid_turn_records = tuple(
            item
            for item in compaction_records
            if item["trigger"] == CompactionTrigger.MID_TURN_FOLLOWUP.value
        )
        mid_turn = mid_turn_records[0] if len(mid_turn_records) == 1 else None
        try:
            skill_mcp_index = skill_turn_trajectory.index(
                "mcp__late__direct_echo"
            )
        except ValueError:
            skill_mcp_index = -1
        required_skill_sequence_seen = bool(
            skill_turn_trajectory[:1] == ("read_file",)
            and skill_turn_trajectory.count("read_file") == 1
            and skill_turn_trajectory.count("mcp__late__direct_echo") == 1
            and skill_mcp_index > 0
            and skill_turn_trajectory[1:skill_mcp_index].count("search_files")
            >= 1
        )
        result = {
            "initial_epoch_revision": initial_epoch.epoch_revision,
            "late_meta_before_compaction": (
                _route_counts(before_compaction)["NEW_MCP_META_ONLY"] == 1
            ),
            "summary_calls": len(summary_records),
            "summary_proofs": tuple(summary_records),
            "first_snapshot_count": len(first_snapshots),
            "final_snapshot_count": len(all_snapshots),
            "snapshot_fingerprints": all_snapshots,
            "successor_epoch_changed": (
                first_successor.epoch_nonce
                != summary_records[0]["old_epoch_nonce"]
            ),
            "successor_routes": _route_counts(first_successor),
            "successor_route_details": _route_details(first_successor),
            "old_meta_ref_stale": old_ref_stale,
            "retained_skill_presence": _runtime_source_presence(
                first_successor, ContextSourceKind.RETAINED_SKILL_CONTEXT
            ),
            "retained_skill_exact_body_seen": _runtime_source_body_contains(
                first_successor,
                ContextSourceKind.RETAINED_SKILL_CONTEXT,
                _MCP_SENTINEL,
            ),
            "skill_final_sentinel": _SENTINEL in skill_result.final_text,
            "read_file_calls": observed_tools.count("read_file"),
            "search_file_calls": observed_tools.count("search_files"),
            "late_direct_calls": observed_tools.count("mcp__late__direct_echo"),
            "second_epoch_changed": (
                second_successor.epoch_nonce != first_successor.epoch_nonce
            ),
            "second_binding_uses_latest_snapshot": (
                len(all_snapshots) == 2
                and second_successor.canonical_frontier.context_base_semantic_identity
                != first_successor.canonical_frontier.context_base_semantic_identity
            ),
            "new_root_retained_skill_presence": _runtime_source_presence(
                second_successor, ContextSourceKind.RETAINED_SKILL_CONTEXT
            ),
            "correction_won": "BLUE" in corrected.final_text,
            "canonical_tool_trajectory": trajectory,
            "tool_attempt_count": len(trajectory),
            "agentic_mid_turn": {
                "exactly_one_mid_turn_compaction": len(mid_turn_records) == 1,
                "same_turn": (
                    mid_turn is not None
                    and mid_turn["turn_id"] == skill_result.turn_id
                ),
                "trigger": None if mid_turn is None else mid_turn["trigger"],
                "disposition": (
                    None if mid_turn is None else mid_turn["disposition"]
                ),
                "snapshot_count_before": (
                    None if mid_turn is None else mid_turn["snapshot_count_before"]
                ),
                "snapshot_count_after": (
                    None if mid_turn is None else mid_turn["snapshot_count_after"]
                ),
                "tool_calls_before_compaction": (
                    None
                    if mid_turn is None
                    else mid_turn["tool_calls_before_compaction"]
                ),
                "tool_names_before_compaction": (
                    ()
                    if mid_turn is None
                    else mid_turn["tool_names_before_compaction"]
                ),
                "same_turn_tool_trajectory": skill_turn_trajectory,
                "post_compaction_tool_suffix": skill_turn_trajectory[2:],
                "required_skill_sequence_seen": required_skill_sequence_seen,
                "model_call_count": skill_result.model_call_count,
                "final_answer_nonempty": bool(skill_result.final_text.strip()),
            },
        }
        result["passed"] = bool(
            result["late_meta_before_compaction"]
            and result["summary_calls"] == 2
            and all(
                item["tool_choice_none"]
                and item["tools_exact"]
                and item["wire_prefix_exact"]
                for item in summary_records
            )
            and result["first_snapshot_count"] == 1
            and result["final_snapshot_count"] == 2
            and result["successor_epoch_changed"]
            and result["successor_routes"]["DIRECT"] == 2
            and result["old_meta_ref_stale"]
            and result["retained_skill_presence"] == "VALUE"
            and result["retained_skill_exact_body_seen"]
            and result["skill_final_sentinel"]
            and result["read_file_calls"] == 1
            and result["search_file_calls"] >= 1
            and result["late_direct_calls"] == 1
            and result["second_epoch_changed"]
            and result["second_binding_uses_latest_snapshot"]
            and result["new_root_retained_skill_presence"] in {None, "CLEARED"}
            and result["correction_won"]
            and result["agentic_mid_turn"]["exactly_one_mid_turn_compaction"]
            and result["agentic_mid_turn"]["same_turn"]
            and result["agentic_mid_turn"]["trigger"]
            == CompactionTrigger.MID_TURN_FOLLOWUP.value
            and result["agentic_mid_turn"]["disposition"] == "COMPACTED"
            and result["agentic_mid_turn"]["snapshot_count_before"] == 0
            and result["agentic_mid_turn"]["snapshot_count_after"] == 1
            and result["agentic_mid_turn"]["tool_calls_before_compaction"] == 2
            and result["agentic_mid_turn"]["same_turn_tool_trajectory"]
            and result["agentic_mid_turn"]["required_skill_sequence_seen"]
            and result["agentic_mid_turn"]["final_answer_nonempty"]
        )
        return result
    finally:
        try:
            await core.shutdown()
        finally:
            host_module.load_mcp_server_configs = original_loader


async def _run_overbound(
    *, settings: PulsaraSettings, workspace: Path
) -> dict[str, object]:
    import pulsara_agent.conversation_kernel.host as host_module

    _write_mcp_config(workspace, (("direct", ("direct_echo",)),))
    original_loader = host_module.load_mcp_server_configs
    host_module.load_mcp_server_configs = lambda **_: _isolated_mcp_configs(workspace)
    core = KernelHostCore.production(settings=settings)
    summary_records: list[dict[str, object]] = []
    try:
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=True,
            ),
            system_prompt=(
                "You are performing a bounded MCP integration check. Use the "
                "advertised catalog route and keep the final answer brief."
            ),
        )
        session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
        ready = await session._mcp_supervisor.wait_for_server_settlement(  # noqa: SLF001
            "direct", timeout_seconds=20
        )
        if ready.value != "READY":
            raise RuntimeError("overbound initial MCP fixture did not become READY")
        await session.run_turn(
            "Acknowledge this preflight briefly without a tool.",
            command_id="command:round5b:overbound-preflight",
        )
        _seed_completed_history(session, segments=15)
        _write_mcp_config(
            workspace,
            (
                ("direct", ("direct_echo",)),
                ("bulk", tuple(f"bulk_{index:02d}" for index in range(48))),
            ),
        )
        await session.reload_mcp_configs(_isolated_mcp_configs(workspace))
        ready = await session._mcp_supervisor.wait_for_server_settlement(  # noqa: SLF001
            "bulk", timeout_seconds=20
        )
        if ready.value != "READY":
            raise RuntimeError("overbound MCP fixture did not become READY")
        _install_summary_recorder(session, summary_records)
        session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
            automatic_enabled=True,
            auto_trigger_ratio=0.30,
            post_compaction_target_ratio=0.20,
            minimum_reclaim_tokens=1,
        )
        final = await session.run_turn(
            "Use bulk_00 from MCP server bulk once with the harmless text value "
            "round5b, following the current catalog route, then report its result.",
            command_id="command:round5b:overbound",
        )
        successor = _current_epoch(session)
        routes = _route_counts(successor)
        trajectory = _tool_trajectory(session)
        result = {
            "summary_calls": len(summary_records),
            "summary_proofs": tuple(summary_records),
            "snapshot_count": len(_snapshot_fingerprints(session)),
            "route_counts": routes,
            "route_details": _route_details(successor),
            "builtin_direct_count": sum(
                not item.provider_name.startswith("mcp__")
                for item in successor.direct_native_projection_set.tool_versions
            ),
            "partial_mcp_direct_count": sum(
                item.provider_name.startswith("mcp__")
                for item in successor.direct_native_projection_set.tool_versions
            ),
            "inspect_calls": trajectory.count("inspect_new_mcp_tool"),
            "meta_use_calls": trajectory.count("use_new_mcp_tool"),
            "canonical_tool_trajectory": trajectory,
            "final_nonempty": bool(final.final_text.strip()),
        }
        result["passed"] = bool(
            result["summary_calls"] == 1
            and result["snapshot_count"] == 1
            and routes["DIRECT"] == 0
            and routes["NEW_MCP_META_ONLY"] == 49
            and result["builtin_direct_count"] > 0
            and result["partial_mcp_direct_count"] == 0
            and result["inspect_calls"] == 1
            and result["meta_use_calls"] == 1
            and result["final_nonempty"]
        )
        return result
    finally:
        try:
            await core.shutdown()
        finally:
            host_module.load_mcp_server_configs = original_loader


async def _run(settings: PulsaraSettings) -> dict[str, object]:
    with TemporaryDirectory(prefix="pulsara-round5b-retained-") as first_dir:
        retained = await _run_retained_and_repeated(
            settings=settings,
            workspace=Path(first_dir),
        )
    with TemporaryDirectory(prefix="pulsara-round5b-overbound-") as second_dir:
        overbound = await _run_overbound(
            settings=settings,
            workspace=Path(second_dir),
        )
    return {
        "schema_version": "round5b-compaction-dogfood.v1",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider_api": settings.llm.api,
        "provider_model": settings.llm.pro.model_id,
        "retained_and_repeated": retained,
        "overbound_mcp": overbound,
        "credentials_prompts_summaries_results_skill_bodies_schemas_or_refs_recorded": False,
        "status": "passed" if retained["passed"] and overbound["passed"] else "failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    load_env_file(args.env_file, override=False)
    initial = PulsaraSettings.from_env()
    admin_root_dsn, database_name, runtime_dsn = _create_database(initial)
    try:
        report = asyncio.run(_run(_runtime_settings(args.env_file, runtime_dsn)))
    except BaseException as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        report = {
            "schema_version": "round5b-compaction-dogfood.v1",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "external_or_runtime_failure",
            "failure_type": type(exc).__name__,
            "failure_message": str(exc)[:512],
            "failure_site": f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}",
            "credentials_prompts_summaries_results_skill_bodies_schemas_or_refs_recorded": False,
        }
    finally:
        _drop_database(admin_root_dsn, database_name)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
