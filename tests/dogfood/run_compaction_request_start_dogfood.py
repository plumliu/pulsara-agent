"""Saved DeepSeek high/max: real summary, request start and post-summary wire.

Uses only saved deepseek-flash connections. Canonical predecessor fixtures are
deterministic; every recorded summary and ordinary call uses the real provider.
The saved settings are read-only and the PostgreSQL database is UUID-isolated.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock, build_prepared_root_turn_intent
from pulsara_agent.identity import random_id
from pulsara_agent.llm.adapters.openai import chat_completions, responses
from pulsara_agent.llm.input import FrozenPromptContent, PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import ModelCallBinding, ReasoningEffortSelection
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore, _RecordingModelRuntime, _RecordingTransport,
)
from tests.support.postgres_database import (
    _load_local_env_if_present, admin_root_dsn,
    create_migrated_postgres_test_database, drop_postgres_test_database,
)


def _rows(session, query, values=()):
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        return connection.execute(query, values).fetchall()


def _seed_history(session, *, segments=5, repetitions=300):
    """Publish deterministic source fixtures through the real canonical writer."""
    repository, guard = session.repository, session._lease.guard
    for index in range(segments):
        intent = build_prepared_root_turn_intent(
            session_id=session.session_id, command_id=random_id("command"),
            turn_id=random_id("turn"), entry_id=random_id("entry"),
            context_binding_revision_id=random_id("binding"),
            permission_snapshot_id=random_id("permission"),
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            canonical_prompt=freeze_canonical_prompt(FrozenPromptContent.text(
                f"Historical source {index}: SOURCE_{index}. "
                + f"Completed investigation {index}; retain the validated conclusion and next step. " * repetitions
            )), occurred_at=datetime.now(timezone.utc), actor_id="compaction-dogfood-fixture",
        )
        repository.accept_root_turn_intent(guard, intent=intent,
            model_resolution_snapshot=session._model_runtime.freeze_resolution_snapshot(),
            deadline_monotonic=monotonic() + 30)
        cut = repository.prepare_provider_input_cut(guard, turn_id=intent.turn_id,
            deadline_monotonic=monotonic() + 30)
        text = f"SOURCE_{index}_FINAL: completed; next step is to continue the current exact request."
        repository.commit_assistant_message(guard, cut=cut, entry_id=random_id("entry"),
            parent_content=InlineContent.from_bytes(text.encode()),
            blocks=(AssistantTextBlock(block_id=random_id("block"), text=InlineContent.from_bytes(text.encode())),),
            complete_turn=True, occurred_at=datetime.now(timezone.utc),
            actor_id="compaction-dogfood-fixture", deadline_monotonic=monotonic() + 30)


async def _finished(session, command):
    while True:
        result = await session.query_command(command)
        if result is not None and result.status != "PENDING":
            assert result.status == "SUCCEEDED", result
            return result
        await asyncio.sleep(0.02)


def _verify_local_database_roots():
    _load_local_env_if_present()
    values = []
    for label, dsn in (("admin", admin_root_dsn()), ("runtime", os.environ.get("PULSARA_POSTGRES_DSN"))):
        if not dsn:
            raise RuntimeError(f"missing test harness {label} PostgreSQL DSN")
        parsed = conninfo_to_dict(dsn)
        if parsed.get("host") not in {"localhost", "127.0.0.1", "::1"} or parsed.get("dbname") != "pulsara":
            raise RuntimeError(f"unverified/nonlocal {label} PostgreSQL root")
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            observed = connection.execute("SELECT current_database(),inet_server_addr()::text,inet_server_port(),current_user").fetchone()
        values.append({"label": label, "dsn": dsn, "physical": observed})
    return values


class _ObservedExecution:
    def __init__(self, delegate, record, on_first):
        self.delegate, self.record, self.on_first = delegate, record, on_first
        self.observed = False

    async def read_next(self):
        item = await self.delegate.read_next()
        if not self.observed:
            self.observed = True
            await self.on_first(self.record)
        if self.record.get("terminal") is not None:
            await self.on_first(self.record)
        return item

    async def request_cancel(self, *, reason):
        await self.delegate.request_cancel(reason=reason)

    async def aclose(self):
        await self.delegate.aclose()

    async def wait_physical_completion(self):
        return await self.delegate.wait_physical_completion()


async def run(destination: Path, selected_cases: tuple[str, ...] = ()):
    saved = LocalSettingsStore().read()
    saved_home = str(require_pulsara_home())
    scrubber = secret_scrubber(saved)
    report = {"saved_home": saved_home, "saved_postgres_present": saved.postgres is not None,
              "status": "preparing", "calls": [], "cases": [],
              "database_roots": _verify_local_database_roots()}
    active = {}

    def save():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(scrubber.scrub_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)) + "\n")

    connections = {c.target.wire_api.value: c for c in saved.model_connections
                   if c.target.model_id == "deepseek-flash" and c.target.route_id == "deepseek"}
    required = ("openai_chat_completions", "openai_responses")
    for wire in required:
        if wire not in connections or saved.model_api_key(connections[wire].id) is None:
            raise RuntimeError(f"missing saved DeepSeek/deepseek-flash connection or credential for {wire}")
    database = create_migrated_postgres_test_database()
    report["database"] = asdict(database)
    prior_home = os.environ.get("PULSARA_HOME")
    original_open = _RecordingTransport.open_stream

    async def on_first(record):
        case, session = active["case"], active["session"]
        if record["purpose"] == "context_compaction_summary" and not case.get("summary_observed"):
            case["summary_observed"] = True
            case["summary_sequence"] = record["sequence"]
            target = session.active_root_turn_id()
            case["summary_barrier"] = {
                "active_turn_id": target,
                "turns": _rows(session, "SELECT id,status,initial_entry_id,current_context_binding_revision_id FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at", (session.session_id,)),
                "pending_queue": _rows(session, "SELECT id,status,delivery_mode,target_turn_id,input_origin,consumed_entry_id FROM pulsara_v3.prompt_queue_items WHERE session_id=%s ORDER BY queue_sequence", (session.session_id,)),
            }
            marker = case["marker"]
            if target is None:
                assert case["kind"] == "idle_manual"
                queued = await session.submit_prompt(command_id=random_id("command"),
                    content=PromptContent.text(f"Reply only {marker}."), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                case["queued_after_manual_admission"] = asdict(queued)
                case["ordinary_command"] = queued.command_id
            else:
                rows = _rows(session, "SELECT status FROM pulsara_v3.turns WHERE session_id=%s AND id=%s", (session.session_id, target))
                assert rows[0]["status"] == "RUNNING"
                queued = await session.submit_prompt(command_id=random_id("command"), content=PromptContent.text(f"Also include {marker} in your final reply."))
                converted = await session.steer_queued_prompt(command_id=random_id("command"),
                    source_queue_item_id=queued.prompt_delivery.queue_item_id, target_turn_id=target)
                case["steer"] = asdict(converted)
                assert converted.status == "PENDING", converted
            save()
        elif (case["kind"] == "turn_auto" and record["purpose"] != "context_compaction_summary"
              and record.get("terminal") is not None and not case.get("auto_armed")):
            case["auto_armed"] = True
            # Arm at the actual first response's terminal boundary. The usage
            # anchor may lower a heuristic estimate before the tool followup.
            measured_input = record["terminal"]["input_tokens"] or record["budget_input_tokens"]
            ratio = measured_input / record["effective_input_budget_tokens"]
            case["auto_trigger_reported_input_tokens"] = measured_input
            session._compaction.policy = replace(session._compaction.policy,
                automatic_enabled=True, auto_trigger_ratio=ratio * 0.95,
                post_compaction_target_ratio=ratio * 0.85, minimum_reclaim_tokens=1)
            save()

    def observed_open(owner, *, call, context):
        execution = original_open(owner, call=call, context=context)
        record = report["calls"][-1]
        record["case"] = active["case"]["name"]
        save()
        return _ObservedExecution(execution, record, on_first)

    def observed_builder(original):
        def build(*, call, context):
            payload = original(call=call, context=context)
            records = [r for r in report["calls"] if r["resolved_model_call_id"] == call.resolved_model_call_id]
            if records:
                records[-1].setdefault("adapter_payloads", []).append(payload)
                save()
            return payload
        return build

    try:
        with TemporaryDirectory(prefix="pulsara-compaction-request-start-") as temporary, ExitStack() as stack:
            root = Path(temporary)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(replace(saved,
                postgres=LocalPostgresConfig(database.runtime_dsn, database.admin_dsn)))
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            report["capabilities"] = []
            for wire, connection in connections.items():
                target = native.freeze_resolution_snapshot().connection(connection.id).target
                efforts = {}
                for effort in ("high", "max"):
                    native.freeze_resolution_snapshot().validate(ModelCallBinding(connection.id, ReasoningEffortSelection(effort)))
                    efforts[effort] = "legal"
                report["capabilities"].append({"wire": wire, "connection_id": connection.id.value,
                    "reasoning": asdict(target.reasoning), "validated": efforts})
            stack.enter_context(patch.object(_RecordingTransport, "open_stream", observed_open))
            stack.enter_context(patch.object(chat_completions, "build_chat_completions_payload", observed_builder(chat_completions.build_chat_completions_payload)))
            stack.enter_context(patch.object(responses, "build_responses_payload", observed_builder(responses.build_responses_payload)))
            core = KernelHostCore.production(model_runtime=_RecordingModelRuntime(native, report["calls"]))
            try:
                cases = (
                    ("chat_high_idle", "openai_chat_completions", "high", "idle_manual"),
                    ("responses_high_first", "openai_responses", "high", "first_call_empty"),
                    ("chat_max_active", "openai_chat_completions", "max", "turn_auto"),
                    ("responses_max_switch", "openai_responses", "max", "cross_protocol_target_switch"),
                )
                for name, wire, effort, kind in cases:
                    if selected_cases and name not in selected_cases:
                        continue
                    case = {"name": name, "wire": wire, "effort": effort, "kind": kind,
                            "marker": f"COMPACTION_{name.upper()}_STEER", "call_start": len(report["calls"])}
                    report["cases"].append(case)
                    session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=workspace))
                    await session.attach_controller("compaction-request-start-dogfood")
                    active.update(case=case, session=session)
                    binding = ModelCallBinding(connections[wire].id, ReasoningEffortSelection(effort))
                    await session.update_model_call_binding(binding)
                    session._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False, minimum_reclaim_tokens=1)
                    _seed_history(session)
                    try:
                        print(f"{name}: starting", flush=True)
                        async with asyncio.timeout(900):
                            if kind == "idle_manual":
                                outcome = await session.compact_context(command_id=random_id("command"))
                                case["compaction"] = asdict(outcome)
                                assert outcome.disposition.value == "COMPACTED", outcome
                                await _finished(session, case["ordinary_command"])
                            else:
                                if kind == "cross_protocol_target_switch":
                                    source = ModelCallBinding(connections["openai_chat_completions"].id, ReasoningEffortSelection("high"))
                                    await session.update_model_call_binding(source)
                                    result = await session.run_turn(PromptContent.text("Reply only SOURCE_CONNECTION_READY."), command_id=random_id("command"))
                                    case["source_reply"] = result.final_text
                                    await session.update_model_call_binding(binding)
                                if kind != "turn_auto":
                                    usage = await session.read_context_usage()
                                    case["before_usage"] = usage
                                    ratio = usage["input_tokens"] / usage["input_budget_tokens"]
                                    session._compaction.policy = replace(session._compaction.policy,
                                        automatic_enabled=True, auto_trigger_ratio=ratio * 0.95,
                                        post_compaction_target_ratio=ratio * 0.85)
                                    command = random_id("command")
                                    case["accepted"] = asdict(await session.submit_prompt(command_id=command,
                                        content=PromptContent.text("Continue the current request. Reply briefly; include any later requested marker."),
                                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS))
                                    await _finished(session, command)
                                else:
                                    result = await session.run_turn(PromptContent.text(
                                        "Use the terminal tool exactly once to run `printf COMPACTION_TOOL_OK`. "
                                        "After the result, reply briefly and include any later requested marker."
                                    ), command_id=random_id("command"), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                                    case["reply"] = result.final_text
                            assert case.get("summary_observed"), "no actual summary provider response"
                            calls = report["calls"][case["call_start"]:]
                            summaries = [c for c in calls if c["purpose"] == "context_compaction_summary"]
                            following = next(c for c in calls if c["sequence"] > case["summary_sequence"] and c["purpose"] != "context_compaction_summary")
                            assert case["marker"] in json.dumps(following["provider_input"], ensure_ascii=False)
                            for call in summaries + [following]:
                                assert call["connection_id"] == connections[wire].id.value
                                assert call["reasoning"] == {"kind": "effort", "value": effort}, call["reasoning"]
                                assert call["terminal"]["kind"] == "COMPLETED", call["terminal"]
                                assert call.get("adapter_payloads"), "missing actual adapter payload"
                                payload = call["adapter_payloads"][-1]
                                lowered = dict(payload)
                                lowered.update(lowered.pop("extra_body", {}))
                                actual_effort = lowered.get("reasoning_effort") or lowered.get("reasoning", {}).get("effort")
                                assert actual_effort == effort, lowered.get("reasoning")
                            case["adoptions"] = _rows(session, "SELECT event_sequence,event_type,payload FROM pulsara_v3.agent_events WHERE session_id=%s AND event_type='CompactionAdopted' ORDER BY event_sequence", (session.session_id,))
                            assert case["adoptions"]
                            case["status"] = "passed"
                    except Exception as error:
                        case["status"] = "failed"
                        case["error"] = f"{type(error).__name__}: {error}"
                        print(f"{name}: failed: {scrubber.scrub_text(case['error'])}", flush=True)
                    finally:
                        case["call_end"] = len(report["calls"])
                        save()
                        await core.close_session(session.host_session_id)
                    if case["status"] == "passed":
                        print(f"{name}: passed", flush=True)
                report["status"] = "passed" if all(c["status"] == "passed" for c in report["cases"]) else "failed"
            finally:
                await core.shutdown()
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if prior_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = prior_home
        drop_postgres_test_database(admin_root_dsn(), database.database_name)
        report["database_dropped"] = True
        save()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[], choices=(
        "chat_high_idle", "responses_high_first", "chat_max_active", "responses_max_switch"))
    arguments = parser.parse_args()
    result = asyncio.run(run(arguments.output.resolve(), tuple(arguments.case)))
    raise SystemExit(0 if result["status"] == "passed" else 1)
