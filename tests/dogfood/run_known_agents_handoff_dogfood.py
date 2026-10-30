"""Real DeepSeek native-agent query after adopted Subagent compaction.

Uses saved credentials, in-memory binding, and a verified local temporary database.
The B response is held at its first transport read so its turn is actually active
while A sends; it is released for normal cleanup. All provider calls remain real.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from ipaddress import ip_address
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ReasoningEffortSelection,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_compaction_request_start_dogfood import _rows
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _RecordingTransport,
)

DEST = Path("output/known-agents-implementation-20261030/provider.json")


class HeldExecution:
    def __init__(self, delegate, gate):
        self.delegate, self.gate, self.first = delegate, gate, True

    async def read_next(self):
        if self.first:
            self.first = False
            await self.gate.wait()
        return await self.delegate.read_next()

    async def request_cancel(self, *, reason):
        self.gate.set()
        await self.delegate.request_cancel(reason=reason)

    async def aclose(self):
        self.gate.set()
        await self.delegate.aclose()

    async def wait_physical_completion(self):
        return await self.delegate.wait_physical_completion()


async def eventually(predicate, timeout=180):
    async with asyncio.timeout(timeout):
        while True:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(0.1)


def make_database(admin_root, runtime_root):
    endpoints = []
    for label, dsn in (("admin", admin_root), ("runtime", runtime_root)):
        parts = conninfo_to_dict(dsn)
        if (
            parts.get("host") not in {"localhost", "127.0.0.1", "::1"}
            or parts.get("dbname") != "pulsara"
        ):
            raise RuntimeError(
                f"{label} DSN must explicitly name a local pulsara database"
            )
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            database, user, address, port, createdb = conn.execute(
                "select current_database(),current_user,inet_server_addr()::text,inet_server_port(),rolcreatedb from pg_roles where rolname=current_user"
            ).fetchone()
            if (
                database != "pulsara"
                or not address
                or not ip_address(address.split("/")[0]).is_loopback
                or (label == "admin" and not createdb)
            ):
                raise RuntimeError(f"{label} database is not a verified local target")
            endpoints.append((address, port))
    if endpoints[0] != endpoints[1]:
        raise RuntimeError(
            "admin and runtime must address the same local database server"
        )
    with psycopg.connect(admin_root, connect_timeout=5, autocommit=True) as conn:
        name = "pulsara_known_agents_" + uuid4().hex
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    admin = make_conninfo(admin_root, dbname=name)
    runtime = make_conninfo(runtime_root, dbname=name)
    try:
        PostgresMigrationRunner(admin_dsn=admin, runtime_dsn=runtime).migrate(
            deadline_monotonic=monotonic() + 240
        )
    except BaseException:
        with psycopg.connect(admin_root, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
        raise
    return admin_root, name, admin, runtime, endpoints[0]


async def run(admin_root, runtime_root, selected_wire=None):
    saved_home = require_pulsara_home()
    saved = LocalSettingsStore(saved_home / "local-settings.yaml").read()
    scrubber = secret_scrubber(saved)
    connections = {
        c.target.wire_api.value: c
        for c in saved.model_connections
        if c.target.model_id == "deepseek-flash" and c.target.route_id == "deepseek"
    }
    for wire in ("openai_chat_completions", "openai_responses"):
        if wire not in connections or saved.model_api_key(connections[wire].id) is None:
            raise RuntimeError(
                f"missing saved DeepSeek connection or credential: {wire}"
            )
    report = {"saved_home": str(saved_home), "cases": [], "calls": []}
    DEST.parent.mkdir(parents=True, exist_ok=True)

    def save():
        DEST.write_text(
            scrubber.scrub_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str)
            )
            + "\n"
        )

    root_dsn, db_name, admin, runtime_dsn, endpoint = make_database(
        admin_root, runtime_root
    )
    report["database"] = {
        "name": db_name,
        "host": endpoint[0],
        "port": endpoint[1],
        "isolated": True,
    }
    prior_home = os.environ.get("PULSARA_HOME")
    gate = asyncio.Event()
    held_case = {"held": False}
    original_open = _RecordingTransport.open_stream

    def held_open(owner, *, call, context):
        execution = original_open(owner, call=call, context=context)
        record = report["calls"][-1]
        # Identify the child's explicit instruction, not other sessions containing the marker.
        if (
            "B_HOLD_ACTIVE_MARKER"
            in json.dumps(record["provider_input"], ensure_ascii=False)
            and record["purpose"] != "context_compaction_summary"
            and not any(tool.name == "run_agent" for tool in context.tools)
            and not gate.is_set()
            and not held_case["held"]
        ):
            record["diagnostic_held_active"] = True
            held_case["held"] = True
            return HeldExecution(execution, gate)
        return execution

    try:
        with (
            TemporaryDirectory(prefix="pulsara-known-agents-") as temporary,
            patch.object(_RecordingTransport, "open_stream", held_open),
        ):
            directory = Path(temporary)
            home, workspace = directory / "home", directory / "workspace"
            home.mkdir()
            workspace.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            core = KernelHostCore.production(
                model_runtime=_RecordingModelRuntime(native, report["calls"])
            )
            try:
                for wire in ("openai_chat_completions", "openai_responses"):
                    if selected_wire and wire != selected_wire:
                        continue
                    gate = asyncio.Event()
                    held_case["held"] = False
                    case = {
                        "wire": wire,
                        "model": "deepseek-flash",
                        "reasoning": "high",
                        "status": "running",
                        "call_start": len(report["calls"]),
                    }
                    report["cases"].append(case)
                    save()
                    root = None
                    try:
                        async with asyncio.timeout(900):
                            root = await core.open_session(
                                HostWorkspaceInput(
                                    workspace_kind="project", workspace_root=workspace
                                )
                            )
                            await root.attach_controller("known-agents-dogfood")
                            binding = ModelCallBinding(
                                connections[wire].id, ReasoningEffortSelection("high")
                            )
                            native.freeze_resolution_snapshot().validate(binding)
                            await root.update_model_call_binding(binding)

                            async def turn(text):
                                return await root.run_turn(
                                    PromptContent.text(text),
                                    command_id=random_id("command"),
                                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                                )

                            async def ids():
                                return await root._io.run(
                                    root.repository.list_root_agent_session_ids,
                                    root_session_id=root.session_id,
                                    deadline_monotonic=monotonic() + 30,
                                )

                            async def create(name, instruction):
                                for attempt in range(2):
                                    before = set(await ids())
                                    result = await turn(
                                        f"Use the native run_agent tool exactly once to create an agent named {name}. Its exact instruction is: {instruction} After acceptance, reply briefly."
                                    )
                                    after = set(await ids())
                                    case.setdefault("creation_attempts", []).append(
                                        {
                                            "name": name,
                                            "attempt": attempt + 1,
                                            "reply": result.final_text,
                                            "new_agents": list(after - before),
                                        }
                                    )
                                    if len(after - before) == 1:
                                        return (after - before).pop()
                                    if len(after - before) != 0:
                                        raise AssertionError(
                                            f"{name}: unexpected new agents {list(after - before)}"
                                        )
                                raise AssertionError(
                                    f"{name}: no agent was created after retry"
                                )

                            c = await create(
                                "Independent_C",
                                "Reply briefly with C_INDEPENDENT_DONE.",
                            )
                            b = await create(
                                "Teammate_B",
                                "B_HOLD_ACTIVE_MARKER. Reply briefly with B_READY.",
                            )

                            async def b_running():
                                state = await root._io.run(
                                    root.repository.read_agent_runtime_state,
                                    session_id=b,
                                    deadline_monotonic=monotonic() + 30,
                                )
                                return (
                                    state
                                    and state.get("current_work_id")
                                    and held_case["held"]
                                )

                            await eventually(b_running)
                            a = await create(
                                "Investigator_A",
                                f"Your teammate is {b}. Write a detailed 2,000-word overview of communication practices and finish with A_FIRST_DONE. Do not use tools.",
                            )

                            async def a_done():
                                rows = await root._io.run(
                                    root.repository.list_agent_works,
                                    root_session_id=root.session_id,
                                    agent_id=a,
                                    limit=10,
                                    deadline_monotonic=monotonic() + 30,
                                )
                                return (
                                    rows and rows[0]["status"] == "completed" and rows
                                )

                            await eventually(a_done)
                            child = core._live_agent_session(a)

                            async def child_idle():
                                return child.active_root_turn_id() is None

                            await eventually(child_idle)
                            usage = await child.read_context_usage()
                            ratio = usage["input_tokens"] / usage["input_budget_tokens"]
                            child._compaction.policy = ResolvedCompactionPolicy(
                                automatic_enabled=True,
                                auto_trigger_ratio=ratio * 0.95,
                                post_compaction_target_ratio=ratio * 0.94,
                                minimum_reclaim_tokens=1,
                            )
                            case.update(
                                root=root.session_id, a=a, b=b, c=c, before_usage=usage
                            )
                            compactions = []
                            planning_errors = []
                            fenced = child._runner.compaction._execute_compaction_fenced

                            async def observe_fenced(**kwargs):
                                try:
                                    return await fenced(**kwargs)
                                except Exception as error:
                                    planning_errors.append(
                                        f"{type(error).__name__}: {error}"
                                    )
                                    raise

                            child._runner.compaction._execute_compaction_fenced = (
                                observe_fenced
                            )
                            execute = child._runner.compaction.execute_active

                            async def observed(**kwargs):
                                result = await execute(**kwargs)
                                compactions.append(result)
                                return result

                            child._runner.compaction.execute_active = observed
                            followup = (
                                "Use native query_agents without a target to recover your teammate, then query that AGENT to find its current WORK and query that WORK for its instruction and status. "
                                "Send that teammate an actual native send_agent_message saying HANDOFF_HELLO. Also use the returned ROOT alias to send ROOT the message ROOT_ALIAS_HELLO. Reply briefly after the tool results."
                            )
                            await turn(
                                f"Use native run_agent exactly once to assign a new Work to the existing agent {a}. Its exact instruction is: {followup} After acceptance, reply briefly."
                            )

                            async def second_done():
                                rows = await root._io.run(
                                    root.repository.list_agent_works,
                                    root_session_id=root.session_id,
                                    agent_id=a,
                                    limit=10,
                                    deadline_monotonic=monotonic() + 30,
                                )
                                return (
                                    len(rows) >= 2
                                    and all(
                                        r["status"] in ("completed", "stopped")
                                        for r in rows
                                    )
                                    and rows
                                )

                            case["works"] = await eventually(second_done)
                            case["compactions"] = [
                                asdict(x.outcome) for x in compactions
                            ]
                            case["summary_calls"] = [
                                call["sequence"]
                                for call in report["calls"][case["call_start"] :]
                                if call["purpose"] == "context_compaction_summary"
                                and call["wire_api"] == wire
                                and call["reasoning"]
                                == {"kind": "effort", "value": "high"}
                                and call.get("terminal")
                                and call["terminal"]["kind"] == "COMPLETED"
                            ]
                            case["planning_errors"] = planning_errors
                            case["adoptions"] = _rows(
                                child,
                                "SELECT event_sequence,event_type,payload FROM pulsara_v3.agent_events WHERE session_id=%s AND event_type='CompactionAdopted' ORDER BY event_sequence",
                                (a,),
                            )
                            case["tool_events"] = _rows(
                                child,
                                "SELECT event_sequence,payload FROM pulsara_v3.agent_events WHERE session_id=%s AND event_type='ToolAttemptAccepted' ORDER BY event_sequence",
                                (a,),
                            )
                            case["trace"] = await asyncio.to_thread(_tool_trace, child)
                            assert any(
                                x.outcome.disposition.value == "COMPACTED"
                                for x in compactions
                            ), case["compactions"]
                            assert case["adoptions"], "no CompactionAdopted event"
                            assert case["summary_calls"], (
                                "no completed real summary call for this protocol and reasoning"
                            )
                            second_turn = case["works"][1]["turn_id"]
                            first_entry = _rows(
                                child,
                                "SELECT min(entry_sequence) AS first FROM pulsara_v3.transcript_entries WHERE session_id=%s AND turn_id=%s",
                                (a, second_turn),
                            )[0]["first"]
                            adoption_sequence = case["adoptions"][0]["event_sequence"]
                            tool_sequences = {
                                event["payload"]["tool_call_id"]: event[
                                    "event_sequence"
                                ]
                                for event in case["tool_events"]
                            }
                            trace = [
                                item
                                for item in case["trace"]
                                if item["request_sequence"] > first_entry
                                and tool_sequences.get(item["tool_call_id"], 0)
                                > adoption_sequence
                            ]
                            for item in trace:
                                item["tool_event_sequence"] = tool_sequences[
                                    item["tool_call_id"]
                                ]
                            case["post_adoption_trace"] = trace
                            listings = [
                                x
                                for x in trace
                                if x["tool_name"] == "query_agents"
                                and not x["arguments"].get("target")
                            ]
                            assert listings and any(
                                b in json.dumps(x["result"])
                                and c not in json.dumps(x["result"])
                                and "ROOT" in json.dumps(x["result"])
                                for x in listings
                            ), listings
                            b_work = next(
                                item["current_work"]["work_id"]
                                for entry in listings
                                for item in entry["result"]["agents"]
                                if item["agent"]["agent_id"] == b
                            )
                            assert any(
                                x["tool_name"] == "query_agents"
                                and x["arguments"].get("target", {}).get("kind")
                                == "WORK"
                                and x["arguments"]["target"]["work_id"] == b_work
                                and x["result"].get("status") == "running"
                                for x in trace
                            ), trace
                            assert any(
                                x["tool_name"] == "send_agent_message"
                                and x["arguments"].get("target", {}).get("agent_id")
                                == b
                                and "queued" in json.dumps(x["result"])
                                for x in trace
                            ), trace
                            assert any(
                                x["tool_name"] == "send_agent_message"
                                and x["arguments"].get("target", {}).get("agent_id")
                                == "ROOT"
                                and x["result"]["recipient"]["agent_id"]
                                == root.session_id
                                for x in trace
                            ), trace
                            case["status"] = "passed"
                    except Exception as error:
                        case["status"] = "failed"
                        case["error"] = f"{type(error).__name__}: {error}"
                        if root:
                            case["root_trace"] = await asyncio.to_thread(
                                _tool_trace, root
                            )
                        print(
                            scrubber.scrub_text(f"{wire}: {case['error']}"), flush=True
                        )
                    finally:
                        gate.set()
                        save()
                        if root:
                            await core.close_session(root.host_session_id)
            finally:
                await core.shutdown()
    finally:
        if prior_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = prior_home
        with psycopg.connect(root_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db_name))
            )
        report["database"]["dropped"] = True
        save()
    return all(x["status"] == "passed" for x in report["cases"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-dsn", required=True)
    parser.add_argument("--runtime-dsn", required=True)
    parser.add_argument(
        "--wire", choices=("openai_chat_completions", "openai_responses")
    )
    args = parser.parse_args()
    raise SystemExit(
        0 if asyncio.run(run(args.admin_dsn, args.runtime_dsn, args.wire)) else 1
    )
