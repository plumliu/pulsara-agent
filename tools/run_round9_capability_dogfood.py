"""Run a content-free Round 9 real-provider capability dogfood.

The probe uses a trusted temporary workspace, a local stdio MCP fixture and an
ephemeral clean-v0 PostgreSQL database.  Its public report contains only closed
route/status values, counts and fingerprints.  It never emits credentials,
DSNs, prompts, tool arguments, tool refs, schemas, replay bodies, or provider
response text.
"""

from __future__ import annotations

from pulsara_agent.llm.input import PromptContent
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
import yaml

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.mcp.naming import mangle_mcp_tool_names
from pulsara_agent.mcp_config import load_mcp_server_configs
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
)
from pulsara_agent.settings import PulsaraSettings, StorageConfig, load_env_file
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput


_APIS = ("openai_chat_completions", "openai_responses")
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "round9_mcp_server.py"


class Round9DogfoodStageFailure(RuntimeError):
    def __init__(
        self,
        stage: str,
        failure_type: str,
        *,
        failure_code: str | None,
        renewal_task_state: str | None,
        elapsed_seconds: float,
    ) -> None:
        self.stage = stage
        self.failure_type = failure_type
        self.failure_code = failure_code
        self.renewal_task_state = renewal_task_state
        self.elapsed_seconds = elapsed_seconds
        super().__init__(f"Round 9 dogfood failed at {stage}: {failure_type}")


def _safe_failure_code(exc: BaseException) -> str | None:
    if isinstance(exc, ProviderModelExecutionFailed):
        return exc.error.code.value
    if isinstance(exc, ProviderModelOutputIncomplete):
        return exc.reason.value
    return None


def _renewal_task_state(session: object | None) -> str | None:
    if session is None:
        return None
    task = getattr(session, "_renewal_task", None)
    if task is None:
        return "ABSENT"
    if not task.done():
        return "RUNNING"
    if task.cancelled():
        return "CANCELLED"
    exception = task.exception()
    return "COMPLETED" if exception is None else type(exception).__name__


def _dsn_with_database(dsn: str, database_name: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = database_name
    return make_conninfo(**values)


def _require_loopback_pulsara(dsn: str, *, label: str) -> None:
    values = conninfo_to_dict(dsn)
    host = str(values.get("host", "")).strip().lower()
    database = str(values.get("dbname", "")).strip()
    if host not in {"localhost", "127.0.0.1", "::1"} or database != "pulsara":
        raise RuntimeError(f"{label} must target the exact loopback database pulsara")


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
    _require_loopback_pulsara(
        settings.storage.postgres_dsn, label="runtime DSN"
    )
    database_name = f"pulsara_round9_{os.getpid()}_{uuid4().hex[:10]}"
    admin_dsn = _dsn_with_database(admin_root_dsn, database_name)
    runtime_dsn = _dsn_with_database(settings.storage.postgres_dsn, database_name)
    with psycopg.connect(admin_root_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    try:
        report = PostgresMigrationRunner(
            admin_dsn=admin_dsn, runtime_dsn=runtime_dsn
        ).migrate(deadline_monotonic=monotonic() + 240)
        if report.migration_head_version != 0:
            raise RuntimeError("Round 9 dogfood clean-v0 install failed")
    except BaseException:
        _drop_database(admin_root_dsn, database_name)
        raise
    return admin_root_dsn, database_name, runtime_dsn


def _settings(env_file: str, *, api: str, runtime_dsn: str) -> PulsaraSettings:
    load_env_file(env_file, override=False)
    os.environ["PULSARA_API"] = api
    os.environ["PULSARA_MEMORY_AUTO_DENSE"] = "false"
    os.environ["PULSARA_MEMORY_EXPLICIT_RERANK"] = "false"
    return replace(
        PulsaraSettings.from_env(),
        storage=StorageConfig(postgres_dsn=runtime_dsn),
    )


def _write_workspace_config(
    workspace: Path,
    *,
    servers: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    raw: dict[str, object] = {"servers": {}}
    configured = raw["servers"]
    assert isinstance(configured, dict)
    for server_id, included in servers:
        configured[server_id] = {
            "enabled": True,
            "required": True,
            "scope_policy": "ROOT_ONLY",
            "supports_parallel_tool_calls": False,
            "catalog_refresh_interval_ms": "DISABLED",
            "exposure_policy": {
                "include_tool_names": list(included),
                "invalid_tool_policy": "FAIL_SERVER",
            },
            "transport": {
                "type": "stdio",
                # Preserve the repository .venv launcher.  Resolving the
                # symlink would select uv's base interpreter and lose the MCP
                # dependency environment owned by this dogfood process.
                "command": os.sys.executable,
                "args": [os.fspath(_FIXTURE)],
            },
        }
    path = workspace / ".pulsara" / "mcp.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(raw, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )


def _isolated_configs(workspace: Path):
    return load_mcp_server_configs(
        workspace_root=workspace,
        user_config_path=workspace / ".missing-user-mcp.yaml",
        trust_workspace_config=True,
    )


def _tool_settlement_summary(
    session,
    *,
    expected_direct_tool_name: str,
    expected_meta_server_id: str | None,
    expected_meta_tool_name: str | None,
    expected_meta_marker: bytes | None,
) -> dict[str, object]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            """
            SELECT e.entry_sequence, b.block_ordinal, b.tool_name,
                   b.tool_arguments,
                   a.id IS NOT NULL AS attempt_accepted,
                   a.remote_identity,
                   r.result_state,
                   r.observation_origin_kind,
                   result_entry.inline_content AS result_content
            FROM pulsara_v3.assistant_message_blocks AS b
            JOIN pulsara_v3.transcript_entries AS e
              ON e.session_id = b.session_id AND e.id = b.assistant_entry_id
            LEFT JOIN pulsara_v3.tool_execution_attempts AS a
              ON a.session_id = b.session_id
             AND a.assistant_entry_id = b.assistant_entry_id
             AND a.tool_call_id = b.tool_call_id
            LEFT JOIN pulsara_v3.tool_results AS r
              ON r.session_id = a.session_id AND r.attempt_id = a.id
            LEFT JOIN pulsara_v3.transcript_entries AS result_entry
              ON result_entry.session_id = r.session_id
             AND result_entry.id = r.result_entry_id
            WHERE b.session_id = %s AND b.block_kind = 'TOOL_CALL'
            ORDER BY e.entry_sequence, b.block_ordinal
            """,
            (session.session_id,),
        ).fetchall()
        states = connection.execute(
            """
            SELECT r.result_state, count(*) AS total
            FROM pulsara_v3.tool_results AS r
            WHERE r.session_id = %s
            GROUP BY r.result_state
            ORDER BY r.result_state
            """,
            (session.session_id,),
        ).fetchall()
    trajectory = tuple(
        {
            "tool_name": str(row["tool_name"]),
            "attempt_accepted": bool(row["attempt_accepted"]),
            "result_state": (
                None if row["result_state"] is None else str(row["result_state"])
            ),
            "arguments_contract_ok": _arguments_contract_ok(
                row,
                expected_direct_tool_name=expected_direct_tool_name,
                expected_meta_server_id=expected_meta_server_id,
                expected_meta_tool_name=expected_meta_tool_name,
            ),
        }
        for row in rows
    )
    direct_marker_verified = any(
        row["tool_name"] == expected_direct_tool_name
        and row["result_state"] == "SUCCESS"
        and row["observation_origin_kind"] == "MCP_REMOTE"
        and bool(row["remote_identity"])
        for row in rows
    )
    meta_marker_verified = (
        None
        if expected_meta_marker is None
        else any(
            row["tool_name"] == "use_new_mcp_tool"
            and row["result_content"] is not None
            and expected_meta_marker in bytes(row["result_content"])
            for row in rows
        )
    )
    return {
        "tool_call_kinds": len({row["tool_name"] for row in rows}),
        "attempts": sum(bool(row["attempt_accepted"]) for row in rows),
        "results": sum(row["result_state"] is not None for row in rows),
        "inspect_attempts": sum(
            bool(row["attempt_accepted"])
            for row in rows
            if row["tool_name"] == "inspect_new_mcp_tool"
        ),
        "meta_use_attempts": sum(
            bool(row["attempt_accepted"])
            for row in rows
            if row["tool_name"] == "use_new_mcp_tool"
        ),
        "meta_use_results": sum(
            row["result_state"] is not None
            for row in rows
            if row["tool_name"] == "use_new_mcp_tool"
        ),
        "canonical_tool_trajectory": trajectory,
        "direct_fixture_result_verified": direct_marker_verified,
        "meta_fixture_result_verified": meta_marker_verified,
        "result_state_counts": {
            str(row["result_state"]): int(row["total"]) for row in states
        },
    }


def _arguments_contract_ok(
    row: dict[str, object],
    *,
    expected_direct_tool_name: str,
    expected_meta_server_id: str | None,
    expected_meta_tool_name: str | None,
) -> bool:
    arguments = row["tool_arguments"]
    if not isinstance(arguments, dict):
        return False
    tool_name = row["tool_name"]
    if tool_name == expected_direct_tool_name:
        return set(arguments) == {"text"} and isinstance(arguments["text"], str)
    if tool_name == "inspect_new_mcp_tool":
        if expected_meta_server_id is None or expected_meta_tool_name is None:
            return False
        provider_name = mangle_mcp_tool_names(
            expected_meta_server_id, (expected_meta_tool_name,)
        )[expected_meta_tool_name]
        return (
            arguments.get("server_id") == expected_meta_server_id
            and arguments.get("tool_name")
            in {expected_meta_tool_name, provider_name}
            and set(arguments) == {"server_id", "tool_name"}
        )
    if tool_name == "use_new_mcp_tool":
        return (
            set(arguments) == {"tool_ref", "arguments"}
            and isinstance(arguments["tool_ref"], str)
            and arguments["tool_ref"].startswith("mcpref_")
            and arguments["arguments"] == {"text": "round9"}
        )
    return False


def _epoch_summary(session) -> dict[str, object]:
    view = session._input_continuity.current_view(  # noqa: SLF001
        ProviderInputContinuityScope(
            session_id=session.session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
    )
    if view is None:
        raise RuntimeError("Round 9 dogfood has no installed provider epoch")
    routes = view.mcp_route_projection.routes
    counts = {"DIRECT": 0, "NEW_MCP_META_ONLY": 0, "UNAVAILABLE": 0}
    reasons: set[str] = set()
    for route in routes:
        counts[route.route.value] += 1
        reasons.add(route.public_reason_code.value)
    return {
        "epoch_revision": view.epoch_revision,
        "wire_tools_fingerprint": view.wire_input_plan.wire_tools_fingerprint,
        "native_projection_fingerprint": (
            view.direct_native_projection_set.projection_set_fingerprint
        ),
        "route_projection_fingerprint": (
            view.mcp_route_projection.projection_fingerprint
        ),
        "route_counts": counts,
        "route_reason_codes": sorted(reasons),
        "replay_fragment_count": len(view.assistant_replay_fragments),
    }


async def _run_scenario(
    *, settings: PulsaraSettings, workspace: Path, scenario: str
) -> dict[str, object]:
    started_at = monotonic()
    if scenario == "direct_late_meta":
        initial_servers = (("direct", ("direct_echo",)),)
        second_server = ("late", ("bulk_00",))
        first_prompt = (
            "Call the direct_echo MCP tool from server direct exactly once with "
            "one short harmless value, then answer briefly."
        )
        second_prompt = (
            "Please execute bulk_00 from MCP server late and report its short "
            "result. It is listed as a new MCP tool rather than a direct native "
            "tool, so inspect its exact schema first and then invoke it using the "
            "returned reference. Use the harmless text value 'round9'."
        )
        expected_tool_names = (
            "mcp__direct__direct_echo",
            "inspect_new_mcp_tool",
            "use_new_mcp_tool",
        )
        expected_direct_tool_name = "mcp__direct__direct_echo"
        expected_meta_server_id = "late"
        expected_meta_tool_name = "bulk_00"
        expected_meta_marker = b"bulk-00:"
    elif scenario == "direct_incompatible_meta":
        initial_servers = (
            (
                "mixed",
                ("direct_echo", "native_incompatible_echo"),
            ),
        )
        second_server = None
        first_prompt = (
            "Call the direct_echo MCP tool from server mixed exactly once with "
            "one short harmless value, then answer briefly."
        )
        second_prompt = (
            "Please execute native_incompatible_echo from MCP server mixed and "
            "report its short result. It is available through the new-MCP path, "
            "so inspect its exact schema first and then invoke it using the "
            "returned reference. Use the harmless text value 'round9'."
        )
        expected_tool_names = (
            "mcp__mixed__direct_echo",
            "inspect_new_mcp_tool",
            "use_new_mcp_tool",
        )
        expected_direct_tool_name = "mcp__mixed__direct_echo"
        expected_meta_server_id = "mixed"
        expected_meta_tool_name = "native_incompatible_echo"
        expected_meta_marker = b"incompatible:"
    elif scenario == "direct_disconnected":
        initial_servers = (("direct", ("direct_echo",)),)
        second_server = None
        first_prompt = (
            "Call the direct_echo MCP tool from server direct exactly once with "
            "one short harmless value, then answer briefly."
        )
        second_prompt = (
            "You MUST issue exactly one call to the still-visible direct_echo "
            "native tool from server direct, even if the catalog says its "
            "physical runtime is unavailable; this call is the typed-gate test. "
            "After its result, acknowledge it briefly and do not try another tool."
        )
        expected_tool_names = (
            "mcp__direct__direct_echo",
            "mcp__direct__direct_echo",
        )
        expected_direct_tool_name = "mcp__direct__direct_echo"
        expected_meta_server_id = None
        expected_meta_tool_name = None
        expected_meta_marker = None
    else:  # pragma: no cover - argparse closes the union
        raise ValueError("unknown Round 9 dogfood scenario")

    _write_workspace_config(workspace, servers=initial_servers)
    import pulsara_agent.conversation_kernel.host as host_module

    original_loader = host_module.load_mcp_server_configs
    host_module.load_mcp_server_configs = (  # type: ignore[assignment]
        lambda **_: _isolated_configs(workspace)
    )
    workspace_input = HostWorkspaceInput(
        workspace_kind="project",
        workspace_root=workspace,
        trust_workspace_mcp_config=True,
    )
    stage = "FIRST_HOST_OPEN"

    def advance(value: str) -> None:
        nonlocal stage
        stage = value
        print(f"round9_dogfood_stage={value}", flush=True)

    core = KernelHostCore.production(settings=settings)
    try:
        advance("FIRST_HOST_OPEN")
        session = await core.open_session(
            workspace_input,
            system_prompt=(
                "You are assisting with a bounded local integration check. "
                "Follow the user request using the available tools and report "
                "the result accurately."
            ),
        )
        advance("DIRECT_TURN")
        first = await session.run_turn(
            PromptContent.text(first_prompt), command_id=f"command:round9:{scenario}:first"
        )
        first_epoch = _epoch_summary(session)

        if scenario == "direct_disconnected":
            # Model an exact known-down physical generation without removing
            # its installed descriptor.  The real provider still performs the
            # second native call; local execution must return typed unavailable
            # and must not fail the Host or mutate tools[].
            session._mcp_supervisor._slots["direct"].mark_dirty()  # noqa: SLF001
        elif second_server is not None:
            advance("LATE_SERVER_RELOAD")
            _write_workspace_config(
                workspace, servers=(*initial_servers, second_server)
            )
            configs = _isolated_configs(workspace)
            await session.reload_mcp_configs(
                configs,
                deadline_monotonic=monotonic() + 30,
            )
            state = await session._mcp_supervisor.wait_for_server_settlement(  # noqa: SLF001
                second_server[0], timeout_seconds=20
            )
            if state.value != "READY":
                raise RuntimeError("late MCP server did not reach READY")

        advance("META_TURN")
        second = await session.run_turn(
            PromptContent.text(second_prompt), command_id=f"command:round9:{scenario}:second"
        )
        second_epoch = _epoch_summary(session)
        settlements = _tool_settlement_summary(
            session,
            expected_direct_tool_name=expected_direct_tool_name,
            expected_meta_server_id=expected_meta_server_id,
            expected_meta_tool_name=expected_meta_tool_name,
            expected_meta_marker=expected_meta_marker,
        )
        session_id = session.session_id
        first_host_summary = {
            "first_model_calls": first.model_call_count,
            "first_tool_calls": first.tool_call_count,
            "second_model_calls": second.model_call_count,
            "second_tool_calls": second.tool_call_count,
            "first_final_utf8_bytes": len(first.final_text.encode("utf-8")),
            "second_final_utf8_bytes": len(second.final_text.encode("utf-8")),
            "wire_tools_stable": (
                first_epoch["wire_tools_fingerprint"]
                == second_epoch["wire_tools_fingerprint"]
            ),
            "first_epoch": first_epoch,
            "second_epoch": second_epoch,
            "canonical_settlement": settlements,
        }
    except BaseException as exc:
        raise Round9DogfoodStageFailure(
            stage,
            type(exc).__name__,
            failure_code=_safe_failure_code(exc),
            renewal_task_state=_renewal_task_state(locals().get("session")),
            elapsed_seconds=round(monotonic() - started_at, 3),
        ) from exc
    finally:
        advance("FIRST_HOST_CLOSE")
        try:
            await core.shutdown()
        except BaseException as exc:
            raise Round9DogfoodStageFailure(
                stage,
                type(exc).__name__,
                failure_code=_safe_failure_code(exc),
                renewal_task_state=_renewal_task_state(locals().get("session")),
                elapsed_seconds=round(monotonic() - started_at, 3),
            ) from exc
        finally:
            host_module.load_mcp_server_configs = original_loader

    # A distinct Host owner rehydrates the same open conversation and sends a
    # normal full-history continuation.  No remote response/session ID is used.
    host_module.load_mcp_server_configs = (  # type: ignore[assignment]
        lambda **_: _isolated_configs(workspace)
    )
    second_core = KernelHostCore.production(settings=settings)
    try:
        advance("FRESH_HOST_OPEN")
        resumed = await second_core.resume_session(
            session_id,
            workspace_input=workspace_input,
            system_prompt=(
                "This is a bounded integration check. Do not call any tool for "
                "this final continuation; answer briefly."
            ),
        )
        advance("FRESH_HOST_CONTINUATION")
        continued = await resumed.run_turn(
            PromptContent.text("Continue this conversation with one short acknowledgement and no tool."),
            command_id=f"command:round9:{scenario}:resume",
        )
        resumed_epoch = _epoch_summary(resumed)
        fresh_host = {
            "completed": bool(continued.final_entry_id),
            "model_calls": continued.model_call_count,
            "tool_calls": continued.tool_call_count,
            "final_utf8_bytes": len(continued.final_text.encode("utf-8")),
            "selected_native_replay_fragments": resumed_epoch[
                "replay_fragment_count"
            ],
        }
    except BaseException as exc:
        raise Round9DogfoodStageFailure(
            stage,
            type(exc).__name__,
            failure_code=_safe_failure_code(exc),
            renewal_task_state=_renewal_task_state(locals().get("resumed")),
            elapsed_seconds=round(monotonic() - started_at, 3),
        ) from exc
    finally:
        advance("FRESH_HOST_CLOSE")
        try:
            await second_core.shutdown()
        except BaseException as exc:
            raise Round9DogfoodStageFailure(
                stage,
                type(exc).__name__,
                failure_code=_safe_failure_code(exc),
                renewal_task_state=_renewal_task_state(locals().get("resumed")),
                elapsed_seconds=round(monotonic() - started_at, 3),
            ) from exc
        finally:
            host_module.load_mcp_server_configs = original_loader

    settlement = first_host_summary["canonical_settlement"]
    actual_tool_names = tuple(
        item["tool_name"] for item in settlement["canonical_tool_trajectory"]
    )
    trajectory_ok = actual_tool_names == expected_tool_names and all(
        item["arguments_contract_ok"]
        for item in settlement["canonical_tool_trajectory"]
    )
    if scenario == "direct_disconnected":
        path_ok = (
            settlement["meta_use_attempts"] == 0
            and settlement["result_state_counts"].get("TOOL_UNAVAILABLE", 0) == 1
            and settlement["direct_fixture_result_verified"]
        )
    else:
        path_ok = (
            settlement["meta_use_attempts"] == 1
            and settlement["meta_use_results"] == 1
            and settlement["direct_fixture_result_verified"]
            and settlement["meta_fixture_result_verified"]
        )
    passed = (
        first_host_summary["first_tool_calls"] >= 1
        and path_ok
        and trajectory_ok
        and first_host_summary["wire_tools_stable"]
        and fresh_host["completed"]
    )
    return {
        "status": "passed" if passed else "semantic_failure",
        "scenario": scenario,
        "api": settings.llm.api,
        "first_host": first_host_summary,
        "fresh_host": fresh_host,
        "remote_response_id_used": False,
        "private_content_credentials_schema_arguments_or_tool_ref_recorded": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--api", choices=_APIS, required=True)
    parser.add_argument(
        "--scenario",
        choices=(
            "direct_late_meta",
            "direct_incompatible_meta",
            "direct_disconnected",
        ),
        required=True,
    )
    args = parser.parse_args()
    load_env_file(args.env_file, override=False)
    os.environ["PULSARA_API"] = args.api
    initial_settings = PulsaraSettings.from_env()
    admin_root_dsn, database_name, runtime_dsn = _create_database(initial_settings)
    try:
        settings = _settings(args.env_file, api=args.api, runtime_dsn=runtime_dsn)
        with TemporaryDirectory(prefix="pulsara-round9-") as directory:
            report = asyncio.run(
                _run_scenario(
                    settings=settings,
                    workspace=Path(directory),
                    scenario=args.scenario,
                )
            )
    except BaseException as exc:
        report = {
            "status": "external_or_runtime_failure",
            "api": args.api,
            "scenario": args.scenario,
            "failure_type": type(exc).__name__,
            "private_content_or_credentials_recorded": False,
        }
        if isinstance(exc, Round9DogfoodStageFailure):
            report["failure_stage"] = exc.stage
            report["underlying_failure_type"] = exc.failure_type
            report["failure_code"] = exc.failure_code
            report["renewal_task_state"] = exc.renewal_task_state
            report["elapsed_seconds"] = exc.elapsed_seconds
    finally:
        _drop_database(admin_root_dsn, database_name)
    report["schema_version"] = "round9-capability-dogfood.v1"
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
